# vision-generation-boilerplate

A boilerplate for generating images offline with FLUX.2, reference photos, and editing instructions defined in Excel.

사진과 엑셀의 편집 지시를 받아, 같은 장면에서 장애물의 배치와 환경을 바꾼 PNG를 만드는 로컬 Python CLI입니다. 예를 들어 거실 사진 한 장에서 **빈 바닥 → 전선 추가 → 책상 다리로 전선 가운데 가림**을 차례로 요청하고, 각 단계의 이미지를 꺼내 볼 수 있습니다.

> **구현 전 README 초안입니다.** 아래 명령과 출력은 계획된 사용 예시입니다. 설치 버전, GPU 메모리 사용량, 실제 생성 이미지는 첫 실행에서 검증할 항목입니다. [단계별 구현 계획](docs/IMPLEMENTATION_STEPS.md)

## 실행 환경과 최초 설치

`vgen prepare`에는 엑셀과 입력 사진만 필요합니다. 준비 단계에서는 GPU와 모델 가중치 없이 계획을 확인할 수 있습니다. `generate`와 `resume`으로 이미지를 만들 때는 CUDA GPU와 로컬에 준비한 FLUX.2 [klein] 4B 모델 폴더를 사용합니다. 세 명령은 최초 설치와 모델 준비가 끝나면 오프라인으로 동작하도록 설계했습니다.

Linux에서의 설치 예시입니다. Python·PyTorch·CUDA·Diffusers의 지원 버전은 실제 추론을 마친 조합으로 확정할 예정입니다.

```bash
git clone https://github.com/sirius-mhlee/vision-generation-boilerplate.git
cd vision-generation-boilerplate
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
vgen --help
```

모델은 인터넷이 되는 컴퓨터에서 한 번 준비합니다. 아래 예시는 공개된 리비전을 지정해 Diffusers가 읽을 모델 폴더를 받습니다. 다른 컴퓨터에서 받았다면 이 폴더를 워크스테이션에 복사하고 그 경로를 `--model-dir`에 지정합니다. [모델 파일 목록](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/tree/e7b7dc27f91deacad38e78976d1f2b499d76a294) · [Hugging Face 다운로드 안내](https://huggingface.co/docs/huggingface_hub/en/guides/cli#download-a-specific-revision)

```bash
python -m pip install huggingface_hub
hf download black-forest-labs/FLUX.2-klein-4B \
  --revision e7b7dc27f91deacad38e78976d1f2b499d76a294 \
  --local-dir models/FLUX.2-klein-4B \
  --exclude 'flux-2-klein-4b.safetensors'
```

`models/FLUX.2-klein-4B/` 아래에 `model_index.json`과 `scheduler/`, `text_encoder/`, `tokenizer/`, `transformer/`, `vae/`가 있어야 합니다. 위 명령은 별도로 배포하는 단일 파일 가중치의 중복 다운로드를 생략합니다. 모델 카드가 안내하는 VRAM은 약 13GB입니다. 8GB GPU에서 가능한 해상도와 CPU 오프로딩 설정은 실측이 필요합니다. [공식 모델 카드](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)

## 직접 실습 · 거실 사진 한 장으로 PNG 세 장 만들기

### 1. 사진과 엑셀 준비

오른쪽 앞에 책상 다리가 보이고 바닥 줄눈을 구분할 수 있는 전방 카메라 사진을 고릅니다. 프로젝트 폴더에 아래처럼 사진을 놓고 `scenarios.xlsx`를 직접 작성합니다. 엑셀에 적는 사진 경로는 **엑셀 파일이 있는 폴더**를 기준으로 합니다. JPG와 PNG를 사용할 수 있습니다.

```text
pilot/
├── scenarios.xlsx
├── guides/
│   └── living01.jpg
└── refs/
    └── black_cable.jpg   # 실제 전선 외형을 참고할 때만 준비
```

첫 번째 시트의 이름은 **장면**입니다. 한 행이 한 환경입니다. 공간·가구, 바닥·이음새, 조명을 비우면 해당 항목은 가이드 사진을 기준으로 삼습니다.

| 장면 ID | 가이드 사진 | 공간·가구 | 바닥·이음새 | 조명 |
|---|---|---|---|---|
| tile01 | guides/living01.jpg | 오른쪽 앞에 책상 다리가 보이는 거실 | 밝은 회색 타일, 가는 대각선 줄눈 | 낮 자연광 |

두 번째 시트의 이름은 **편집**입니다. 한 행이 한 단계이며 모든 장면에 적용됩니다. 아래 예시는 참고 사진 칸을 비워 가이드 한 장으로 시작합니다.

| 편집 ID | 편집 원본 | 요청 장애물 | 참고 사진 | 배치·가림 지시 |
|---|---|---|---|---|
| A | 가이드 사진 | 없음 | | 바닥 위의 이동식 장애물을 치운다. 책상 다리와 타일 줄눈이 보이게 한다. |
| B | A의 결과 | 전선 | | 오른쪽 줄눈 옆에 검은 전선 한 가닥을 나란히 놓는다. 전선 가운데는 책상 다리 바로 앞에 두고 양끝이 모두 보이게 한다. |
| C | B의 결과 | 전선 | | 책상 다리는 그대로 두고 같은 전선의 가운데만 다리 뒤로 보낸다. 양쪽 끝은 계속 보이게 한다. |

실제 전선의 외형을 쓰려면 B행의 `참고 사진`에 `refs/black_cable.jpg`를 적습니다. 이때 첫 번째 모델 입력은 같은 장면·반복의 `A.png`이고, 두 번째 입력은 전선 사진입니다. C의 입력은 같은 반복에서 만든 `B.png`입니다. `요청 장애물`에서 `없음`은 빈 바닥을 요청하는 값이고, 빈칸은 편집 문장에 맡긴다는 뜻입니다.

### 2. 생성할 계획 확인

프로젝트 루트에서 실행합니다. `--model-dir`에는 나중에 추론에 쓸 폴더의 경로를 적습니다.

```bash
vgen prepare pilot/scenarios.xlsx \
  --out runs/pilot01 --repeat 1 --seed 42 \
  --model-dir "$PWD/models/FLUX.2-klein-4B"
```

터미널 요약의 예시입니다.

```text
장면 1개 × 편집 3개 × 반복 1회 = 예상 PNG 3장
저장 위치  runs/pilot01/images/tile01/r001/
A  guides/living01.jpg → A.png
B  A.png → B.png
C  B.png → C.png
```

`runs/pilot01/plan.json`에서 작업마다 최종 편집 문장, 입력 사진, 시드, 출력 경로를 확인합니다. 예를 들어 C의 원본이 같은 반복의 `B.png`인지 봅니다. 내용을 바꾸려면 엑셀을 수정하고 첫 생성 전에 같은 `prepare` 명령을 다시 실행합니다. 준비에 실패하면 이전에 저장된 계획이 유지됩니다.

### 3. 생성하고 결과 확인

```bash
vgen generate runs/pilot01
```

```text
runs/pilot01/
├── plan.json          # 확인한 작업 순서와 최종 프롬프트
├── run.json           # 계획 해시, 로컬 모델 파일별 SHA-256, 모델·라이브러리 버전
├── manifest.jsonl     # 작업별 결과와 이미지 해시
└── images/tile01/r001/
    ├── A.png
    ├── B.png
    └── C.png
```

가이드와 PNG 세 장을 나란히 봅니다. A에서는 낮은 카메라 시점과 책상 다리, 줄눈이 유지됐는지 봅니다. B에서는 줄눈과 구분되는 전선 한 가닥과 양끝을 확인합니다. C에서는 책상 다리가 같은 위치에 있고 그 뒤로 전선 가운데만 가려졌는지 확인합니다.

### 4. 중단된 생성 이어가기

```bash
vgen resume runs/pilot01
```

A의 성공이 기록되고 B에서 중단됐다면 A를 재사용해 B부터 이어갑니다. B의 PNG를 저장했어도 성공 기록을 남기기 전에 꺼졌다면 B를 다시 만듭니다. 남은 작업이 있을 때는 가중치·토크나이저·설정의 해시를 첫 실행과 대조합니다. 파일이 바뀌었다면 표시된 파일을 복원해 이어가거나, 새 모델을 쓸 실행 폴더로 `prepare`합니다. 모두 끝난 폴더에서는 이미지 수와 저장 위치를 보여 줍니다.

## 내 장면에 맞게 바꾸기

| 만들고 싶은 결과 | 엑셀이나 명령에서 바꿀 부분 | 예상 결과 |
|---|---|---|
| 슬리퍼 색만 바꾸기 | 슬리퍼가 있는 가이드로 장면 1개를 만들고 공간·바닥·조명을 비웁니다. 편집 1행의 원본은 `가이드 사진`, 지시는 “같은 위치의 슬리퍼를 노란색으로 바꾼다”로 적습니다. | 1회에 PNG 1장 |
| 전선과 양말을 같은 배경에서 비교 | 위 예시에 D행을 추가하고 원본은 `A의 결과`, 요청 장애물은 `양말`, 지시는 “빈 바닥 오른쪽에 양말 한 짝을 놓는다”로 적습니다. | 1회에 A·B·C·D 4장. B와 D는 같은 A를 사용 |
| 바닥 10종에 A·B·C 적용 | 장면 시트에 서로 다른 ID로 바닥 10종을 적고 편집은 A·B·C 세 행을 사용합니다. | 1회에 30장, `--repeat 100`이면 3,000장 |

`편집 원본`은 직접 입력할 수 있습니다. 엑셀에서 드롭다운을 쓰는 경우, 새 편집 D를 다음 행의 원본으로 고르려면 해당 열의 데이터 유효성 검사 목록에 `D의 결과`를 추가합니다. D행 자체의 원본은 위 분기 예시에서 `A의 결과`입니다.

## 구현 후 검증할 항목

| 환경 | 확인할 동작 |
|---|---|
| GPU 없는 개발 환경 | 편집 행을 C·A·B 순으로 적어도 계획이 A → B → C로 정렬되는지 확인합니다. 테스트용 생성기로 B에서 중단한 뒤 재개하면 A의 해시와 수정 시각이 유지되고 B·C만 생성돼야 합니다. |
| 네트워크를 끊은 GPU 워크스테이션 | 위 실습을 한글 지시로 실행합니다. 별도 실행에서 B에 정사각형 전선 참고 사진을 추가하고, 두 실행 모두 가이드의 가로 화면비와 의도한 가림이 유지되는지 확인합니다. 해상도, 최대 VRAM, 장당 소요 시간, 사용한 오프로딩 설정을 함께 기록합니다. |
