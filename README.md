# rein (고삐)

`Agent = Model + Harness`

프레임워크 비종속 AI 에이전트 안전성/관측성 미들웨어. `pip install rein`


> 본격적인 README는 M3(OSS 트랙)에서 작성한다. 지금은 개발 환경
> 세팅 안내만 담는다. 프로젝트 전체 설계는 `CLAUDE.md` 참고.

## 개발 환경 세팅

```bash
git clone https://github.com/SeoYeongBaek/rein.git
cd rein
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install
pytest
```

## 레이아웃

```
src/rein/
  harness.py      # 공개 API (Harness) 
  cli.py           # rein seed / replay / rule-from / report
  guardrails/      # 가드레일 파이프라인 
  events/          # 이벤트 저장소 (JSONL) 
  adapters/        # 모델 어댑터 
  replay/          # 리플레이 엔진 
  rules/           # 규칙 생성 엔진 
tests/
```
