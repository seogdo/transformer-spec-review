# 핵심 코드 설명

최종 제출에서 사용한 **OpenAI API · `gpt-4o-mini`**의 텍스트 AI 처리 경로를 설명합니다. 정형 D01·D02 예시는 AI 없이 코드를 실행하는 별도 경로이며, Ollama는 선택 가능한 로컬 경로입니다. 아래 발췌는 실제 소스의 일부이며, 각 함수 전체를 대신하지 않습니다.

## AI 제공업체 선택 경계

[실행 도구](../scripts/run_web.py)는 `.env`의 허용된 값만 읽고 `--backend openai` 또는 `--backend ollama`를 선택한 뒤 앱을 불러옵니다. 앱이 생성될 때 제공업체·모델 설정을 고정하여 이후 요청에서 환경변수를 바꿔 다른 실행에 영향을 주지 않습니다. 같은 HTML이 서버의 설정을 표시하며, 브라우저에 API 키를 전달하지 않습니다.

[연결 모듈의 openai_request](../src/handoff_review/llm_backend.py#L199)는 JSON 스키마 요청을 Chat Completions 형식으로 변환하고, 완료·거절·응답 크기를 확인한 뒤 구조화 객체를 반환합니다. 고정된 `api.openai.com/v1/chat/completions`에 `json_schema`, `strict:true`, `store:false`를 보내며 Ollama 전용 옵션을 그대로 보내지 않습니다. 원문과 인용을 대조하는 검증 및 비교 판정은 두 방식이 공유합니다. Structured Outputs는 출력 형식을 제한할 뿐, 내용 오류까지 제거하지 않습니다. 사양값의 사실성이나 공학적 적합성은 별도 검증 대상입니다. [공식 구조화 출력 안내](https://developers.openai.com/api/docs/guides/structured-outputs)

화면·서버 양쪽에서 OpenAI 외부 처리·과금 동의를 확인합니다. AI 실패 시 자동 재시도나 다른 제공업체로의 전환 없이 보류하며, 실행 당시의 제공업체·모델과 고정 응답 주입 여부를 저장합니다. 저장 기록 조회에는 현재 서버의 모델 이름을 덮어쓰지 않습니다. [실행·검증 구분](ai-backends.md)을 참고하세요.

아래 행 번호는 2026년 10월 21일 OpenAI 연결 반영 소스를 확인한 위치입니다. 이후 코드가 바뀌면 함수 이름으로 다시 찾습니다. 과거 Ollama F01~F06과 F04 고정 응답 재생 측정은 OpenAI 실행 결과가 아닙니다.

## Agent 실행 구조와 호출 제어

발표의 Agent Graph 설명은 [extract_upload](../src/handoff_review/extraction_agent.py#L574)에 직접 구현한 **Python 제어 흐름**입니다. LangGraph의 `StateGraph`, Multi-Agent 또는 MCP 실행 그래프는 아닙니다.

| 순서 | 실행 주체와 동작 | 보류·중단 조건 |
| --- | --- | --- |
| 1 | 코드가 PDF 원문과 고객사를 확인합니다. | 고객사 누락·상충·혼입이면 추출을 시작하지 않습니다. |
| 2 | AI가 문서·항목별 검색 계획을 제안합니다. | 계획은 지정 문서·항목 범위로 검증합니다. |
| 3 | 코드가 키워드·별칭으로 원문을 검색합니다. | 근거가 없으면 두 번째 AI 호출을 생략합니다. |
| 4 | AI가 반환된 근거로 값·단위·인용을 제안합니다. | 인용·값·조건 검증에 실패한 제안은 채택하지 않습니다. |
| 5 | 담당자가 적용할 변경을 확인하고 코드가 사양을 비교합니다. | 출처·조건이 불확실하면 `NEEDS_REVIEW`로 남깁니다. |
| 6 | 검토 결과와 근거를 보관하고 보고서를 엽니다. | 설계 승인이나 실제 제작 지시를 실행하지 않습니다. |

[607~618행의 호출 함수](../src/handoff_review/extraction_agent.py#L607)는 추출 작업 한 번의 모델 요청을 **최대 2회**, 전체 시간 예산을 **90초**로 제한합니다. 항상 두 번 호출되는 것은 아닙니다. `calls_succeeded`는 완료된 구조화 응답을 받은 횟수이고, `accepted_fields`는 이후 원문 검증을 통과한 제안 수이므로 서로 다른 지표입니다. 별도 버튼인 ‘AI 구조 확인’은 추가 요청 경로이므로 서비스 전체 호출 수와 구분합니다. 검색 함수는 프로젝트 내부 함수 호출이며 MCP 서버에 요청하지 않습니다. 실행 추적 목록도 직접 기록한 로컬 정보이며 LangSmith가 아닙니다.

## 근거 검색

위치: [extraction_agent.py의 검색 함수](../src/handoff_review/extraction_agent.py#L301), 발췌는 315~326행입니다.

AI가 지정된 문서·항목·별칭에서 검색 계획을 제안합니다. 코드는 항목별 원문 구간을 찾아 검색어 포함 점수로 정렬하고, 문자 예산 안에서 최대 2개를 반환합니다.

```python
            score = 1 + sum(term in text.casefold() for term in terms)
            ranked.append((score, occurrence))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["page"], pair[1]["start"]))
        for _, occurrence in ranked[:2]:
            text = occurrence["text"][:min(MAX_SNIPPET_CHARACTERS, query_budget)]
            if not text:
                continue
            budget -= len(text)
            query_budget -= len(text)
            hits.append({"document_id": query["document_id"], "field": query["field"],
                         "page": occurrence["page"], "text": text,
                         "chunk_id": f"chunk-{len(hits) + 1}"})
```

- 입력은 문서 ID·검토 항목·검색어와 별칭으로 찾은 원문 구간입니다.
- 출력은 문서·항목·페이지·본문·근거 식별자를 포함한 검색 결과입니다.
- 검색 결과가 없으면 두 번째 AI 호출을 생략하고 확인 필요 상태를 유지합니다. 검색 실패를 항목 없음으로 단정하지 않습니다.
- 검색 결과를 다음 AI 추출 요청에 전달하는 제한된 검색 기반 방식입니다. 임베딩 벡터 검색이나 MCP를 사용하지 않았습니다.

## 값과 원문 검증

발표의 R04 사례는 결선그룹·냉각방식의 **열거값**을 비교합니다. [531~541행](../src/handoff_review/extraction_agent.py#L531)에서 지원하는 별칭을 `canonical_enum_value`로 정규화하여 원문 토큰과 제안이 같은 단일 값인지 확인합니다. 이후 공통 검증 547~552행이 원문·인용·항목 직접 연결을 검사합니다. PPT 7장은 원문 인용 확인 508~509행과 검증 실패 시 미확정 처리 684~687행을 발췌했으며, 686행 경고 기록은 생략했습니다. Dyn11과 Dyn1, OFAF와 ONAF는 서로 다른 값으로 유지되며 같은 값으로 보정하지 않습니다.

위치: [extraction_agent.py의 제안 검증 함수](../src/handoff_review/extraction_agent.py#L498), 아래는 수치 검증 분기의 545~552행입니다.

```python
        matching = [token for token in tokens if token == (proposal["value"], proposal["unit"])]
        quoted = [token for token in quote_tokens if token == (proposal["value"], proposal["unit"])]
    if not matching:
        return "AI 값·단위가 원문의 단일 결합 표기와 일치하지 않습니다."
    if not quoted:
        return "AI 인용에 제안된 값·단위가 함께 존재하지 않습니다."
    if not any(_direct_binding(field, value, unit, quote) for value, unit in quoted):
        return "AI 값·단위가 해당 항목명에 직접 연결된 단일 표기가 아닙니다."
```

원문에서 숫자를 발견했다는 사실만으로 값을 채택하지 않습니다. AI 제안의 값과 단위가 실제 원문과 인용에 함께 있으며, 해당 사양 항목에 직접 연결됐는지 확인합니다.

함수의 앞부분은 페이지 원문과 실제 검색 결과에 인용이 포함됐는지 검사합니다. 원문 항목 전체와 가까운 후속 줄에 `미확정`, `잠정`, `아닙니다` 등의 표현이 있으면 모델이 해당 표현을 인용에서 생략해도 확정하지 않습니다. 다른 항목·표 행 경계를 넘어 유보 상태를 전파하지 않습니다. 열거값은 별도 분기에서 처리합니다. 뒤에서는 여러 값이나 조건 충돌도 검사합니다. 검사에 실패한 제안은 거절 이유와 함께 `EXTRACTION_UNCERTAIN`으로 남습니다. 이 방식은 확정 오류를 피하도록 설계했지만 정상적인 복잡한 표현도 보류할 수 있습니다.

## 변경 적용과 사양 비교

위치: [review.py의 검토 함수](../src/handoff_review/review.py#L40), 아래는 189~200행입니다.

```python
                actual = normalize(spec, actual_entry)
                if actual_source is None:
                    raise UncertainValue("인계값의 출처·인용문·양의 페이지 번호를 확인할 수 없습니다.")
                item["actual"] = {"value": actual.value, "unit": actual.unit, "source": actual_source}
                if expected.condition != actual.condition:
                    raise UncertainValue("부품·운전조건 등 비교 조건이 같지 않거나 한쪽에서만 명시됐습니다.")
                if expected.comparable == actual.comparable:
                    item.update(status="MATCH", reason="명시된 동일 항목·조건에서 단위 정규화 후 값이 같습니다.")
                else:
                    item.update(status="MISMATCH", reason="명시된 동일 항목·조건에서 단위 정규화 후 값이 다릅니다.")
        except UncertainValue as exc:
            item.update(status="NEEDS_REVIEW", reason=str(exc))
```

비교 전에 코드가 변경 문서의 승인 메타데이터·프로젝트 일치·명시적 `supersedes` 관계를 검사하고 기준값을 구성합니다. 날짜가 최신이라는 이유만으로 변경을 적용하지 않습니다. 여기서 승인 표시는 가상 메타데이터이며 실제 결재 권한 인증이 아닙니다.

발췌 부분에서는 인계값의 단위를 정규화하고 출처·운전조건을 확인한 뒤 기준값과 비교합니다. 같은 값은 `MATCH`, 다른 값은 `MISMATCH`입니다. 값·출처·조건이 불확실하면 예외를 받아 `NEEDS_REVIEW`로 남깁니다. 확정 기준에 비해 인계값이 명시적으로 없는 `MISSING` 처리는 직전 분기에 있습니다.

## 의존성 주입과 자동검사

[extract_upload](../src/handoff_review/extraction_agent.py#L574)는 `request_fn`과 서버 시작 시 고정한 `ai_config`를 받습니다. [600~601행](../src/handoff_review/extraction_agent.py#L600)은 주입 함수가 있으면 이를 사용하고, 없으면 설정에 따라 OpenAI 요청 또는 Ollama 요청을 선택합니다. 최종 제출 실행은 OpenAI이며, 시험은 고정 JSON을 반환하는 함수를 주입합니다. 이를 통해 실제 API 과금 없이 호출 횟수·인용 불일치·단위 오류·시간 초과·입력 보존을 재현할 수 있습니다.

[고정 응답 검사](../tests/test_extraction_agent.py#L127)는 2회 호출, 검색 결과의 다음 요청 전달, 원문과 메타데이터 보존을 확인합니다. [OpenAI 연결 분기 검사](../tests/test_extraction_agent.py#L85)는 통신을 모의 함수로 바꿔 설정 전달과 기존 원문 검증을 검사하고, [주입 실행 출처 검사](../tests/test_extraction_agent.py#L102)는 이를 실모델 호출로 표시하지 않는지 확인합니다. 자동검사 통과는 해당 코드 조건의 검증이며 모델의 현업 정확도는 아닙니다.

## 발표에서 설명할 핵심

“AI는 원문에서 근거와 값을 제안합니다. 코드가 인용·값·단위를 검증하고, 담당자가 확인한 변경 관계로 기준을 만든 뒤 사양을 비교합니다. 근거가 부족하면 보류하며, 최종 설계 판단은 담당자가 수행합니다.”

현재처럼 문서 형식을 제한하면 규칙만으로 처리할 수 있는 범위도 있습니다. 2회 모델 호출 구조가 단일 호출이나 규칙 방식보다 유리하다는 결과는 아직 없으며, 같은 문서로 정확도와 지연을 비교하는 후속 실험이 필요합니다.
