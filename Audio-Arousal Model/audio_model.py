import math
import torch
import torch.nn as nn
import torch.nn.functional as F

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class AudioExtractor(nn.Module):
    # Audio Input Feature Extraction
    def __init__(self):
        super().__init__()
        """
        <Input>
        Audio Input -> torch.Size([2, 512, 128])
        
        128이 Frequency, 512가 음성의 길이(Time)에 해당
        
        <기본 아이디어>
        Frequency에 대해서 길이 4의 Kernel을 사용해 인접한 Frequency 간의 관계 특징값 추출
        Time에 대해서 길이 1의 Kernel을 사용해 음성 데이터의 시간 정보가 보존되도록 특징값 추출
        
        <Output>
        4회의 Convolution Layer를 거쳐 torch.Size([24, 512, 32])의 Feature Map 추출
        이를 torch.Size([512, 768])로 쌓아 기존 128차원이었던 Frequency를 768차원으로 확장함.
        추후 Text Embedding Vector의 차원과 동일한 차원으로 설정할 수 있다는 장점 또한 존재함.
        """
        self.conv1 = nn.Conv2d(in_channels=2, out_channels=8, kernel_size=(1, 4), padding=(0, 1), stride=(1, 2), bias=False)
        self.conv2 = nn.Conv2d(in_channels=8, out_channels=8, kernel_size=(1, 7), padding=(0, 3), stride=1, bias=False)
        self.conv3 = nn.Conv2d(in_channels=8, out_channels=24, kernel_size=(1, 4), padding=(0, 1), stride=(1, 2), bias=False)
        self.conv4 = nn.Conv2d(in_channels=24, out_channels=24, kernel_size=(1, 7), padding=(0, 3), stride=1, bias=False)

        nn.init.xavier_uniform_(self.conv1.weight)
        nn.init.xavier_uniform_(self.conv2.weight)
        nn.init.xavier_uniform_(self.conv3.weight)
        nn.init.xavier_uniform_(self.conv4.weight)

    def forward(self, x):
        batch_size = x.size(0)

        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = self.conv4(x)
        x = x.transpose(2, 1)
        x = x.reshape([batch_size, 512, -1])

        return x


def padding_mask_audio(audio):
    """
    음성 데이터의 Zero Padding 부분 Masking Tensor 생성
    :param audio: 음성 데이터
    :return: torch.Size([batch_size, 1, 512])
    """
    return torch.all(audio != 0, axis=2).unsqueeze(-2)


class FFN(nn.Module):
    def __init__(self, hidden_dim=768, inner_dim=1024, dropout=0.1):
        """
        :param hidden_dim: input의 차원
        :param inner_dim: FC Layer 차원
        :param dropout: Dropout
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self.inner_dim = inner_dim

        self.fc1 = nn.Linear(hidden_dim, inner_dim)
        self.fc2 = nn.Linear(inner_dim, hidden_dim)
        self.relu = nn.ReLU(inplace=False)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, x):
        res = x
        x = self.relu(self.fc1(x))
        x = self.dropout(x)
        x = self.fc2(x)
        output = x + res

        return output


def Self_Attention(Q, K, V, mask=None):
    """
    Self Attention
    :param Q: Query
    :param K: Key
    :param V: Value
    :param mask: 음성 Masking Tensor
    :return: Attention이 적용된 데이터 반환
    """
    K_t = torch.transpose(K, -2, -1)
    KV = torch.matmul(Q, K_t)
    dim = Q.size()[-1]
    drop = nn.Dropout(p=0.1)

    score = KV / math.sqrt(dim)
    if mask is not None:
        mask = mask.unsqueeze(1)
        score = torch.masked_fill(score, ~mask, -1e9)

    score = drop(F.softmax(score, dim=-1))

    att_value = torch.matmul(score, V)

    return att_value, score


class MHA(nn.Module):
    def __init__(self, hidden_dim=768, num_head=8, dropout=0.1, device=device):
        """
        Multi Head Attention
        :param hidden_dim: 위와 동일
        :param num_head: Multi Head의 Head 수
        :param dropout:
        :param device:
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_head = num_head
        self.head_dim = hidden_dim // num_head
        self.scale = torch.sqrt(torch.FloatTensor()).to(device)

        self.Q_fc = nn.Linear(self.hidden_dim, self.hidden_dim, bias=False)
        self.K_fc = nn.Linear(self.hidden_dim, self.hidden_dim, bias=False)
        self.V_fc = nn.Linear(self.hidden_dim, self.hidden_dim, bias=False)
        self.Out_fc = nn.Linear(self.hidden_dim, self.hidden_dim, bias=False)

        self.dropout = nn.Dropout(p=dropout)

    def forward(self, Q_input, K_input, V_input, mask=None):
        batch_size = Q_input.size(0)

        Q = self.Q_fc(Q_input).view(batch_size, -1, self.num_head, self.head_dim).transpose(1, 2)
        K = self.K_fc(K_input).view(batch_size, -1, self.num_head, self.head_dim).transpose(1, 2)
        V = self.V_fc(V_input).view(batch_size, -1, self.num_head, self.head_dim).transpose(1, 2)

        att_value, score = Self_Attention(Q, K, V, mask=mask)
        att_value = att_value.transpose(1, 2).contiguous().view(batch_size, -1, self.hidden_dim)

        output = self.Out_fc(att_value)

        return output


class AELayer1(nn.Module):
    def __init__(self, hidden_dim=768, num_head=8, inner_dim=1024):
        """
        Multi Head Attention 학습 Block의 개별 Layer
        :param hidden_dim: 위와 동일
        :param num_head: 위와 동일
        :param inner_dim: 위와 동일
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_head = num_head
        self.inner_dim = inner_dim

        self.MHA = MHA(self.hidden_dim, self.num_head)
        self.layerNorm1 = nn.LayerNorm(self.hidden_dim)
        self.layerNorm2 = nn.LayerNorm(self.hidden_dim)
        self.ffn = FFN(self.hidden_dim, self.inner_dim)

        self.dropout1 = nn.Dropout(p=0.1)
        self.dropout2 = nn.Dropout(p=0.1)

    def forward(self, x, mask=None):
        output = self.MHA(x, x, x, mask)
        output = x + self.dropout1(output)
        output = self.layerNorm1(output)

        output_ = self.ffn(output)
        output = output + self.dropout2(output_)
        output = self.layerNorm2(output)

        return output


class AEBlock1(nn.Module):
    def __init__(self, hidden_dim=768, num_head=8, inner_dim=1024, n_layers=4):
        """
        Audio와 Masking을 바탕으로 Self Attention 학습
        :param hidden_dim: 위와 동일
        :param num_head: 위와 동일
        :param inner_dim: 위와 동일
        :param n_layers: AELayer 반복 횟수 지정
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_head = num_head
        self.inner_dim = inner_dim

        self.AE1 = nn.ModuleList([AELayer1(hidden_dim=self.hidden_dim,
                                           num_head=self.num_head,
                                           inner_dim=self.inner_dim) for i in range(n_layers)])

    def forward(self, x):
        masking = padding_mask_audio(x)

        for layer in self.AE1:
            x = layer(x, masking)

        return x


class AudioRegressor(nn.Module):
    def __init__(self, hidden_dim=768, num_head=4, inner_dim=1536, n_layers=6):
        """
        1) AudioExtractor       : 음성의 시간 정보 보존 + 인접한 Frequency의 값들 간의 관계적 특징 추출
        2) Multi-head Attention : Arousal 예측을 위한 Self Attention
        3) GRU                  : 시간 정보 보존 + Masking -> Sequence Data의 특징을 살려 Arousal 예측하기 위해 GRU 사용

        :param hidden_dim: 위와 동일
        :param num_head: 위와 동일
        :param inner_dim: 위와 동일
        :param n_layers: 위와 동일
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_head = num_head
        self.inner_dim = inner_dim
        self.n_layers = n_layers

        self.Extractor = AudioExtractor()
        self.AEBlock1 = AEBlock1(self.hidden_dim, self.num_head, self.inner_dim, self.n_layers)

        self.gru = nn.GRU(input_size=self.hidden_dim, hidden_size=64, num_layers=2, bias=False, batch_first=True, dropout=0.1)
        self.fc = nn.Linear(64, 1)

        nn.init.xavier_uniform_(self.fc.weight)

    def forward(self, audio):
        batch_size = audio.size(0)

        audio_output = self.Extractor(audio)
        audio_output = self.AEBlock1(audio_output)

        h0 = torch.zeros(2, batch_size, 64).requires_grad_().to(device)

        out, h = self.gru(audio_output, h0)
        h_t = out[:, -1, :]
        output = self.fc(h_t)

        return output, audio_output