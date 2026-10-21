# 핵심 코드 설명

텍스트 AI 모드의 처리 경로를 설명합니다. 정형 D01·D02 예시는 AI 없이 코드를 실행하는 별도 경로입니다. 아래 발췌는 실제 소스의 일부이며, 각 함수 전체를 대신하지 않습니다.

## 근거 검색

위치: [extraction_agent.py의 검색 함수](../src/handoff_review/extraction_agent.py#L300)

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

위치: [extraction_agent.py의 제안 검증 함수](../src/handoff_review/extraction_agent.py#L497), 아래는 수치 검증 분기의 544~551행입니다.

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

위치: [review.py의 검토 함수](../src/handoff_review/review.py#L113), 아래는 189~200행입니다.

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

[extract_upload](../src/handoff_review/extraction_agent.py#L573)는 `request_fn`을 받습니다. 기본 실행은 로컬 모델 요청 함수를 사용하며, 시험은 고정 JSON을 반환하는 함수를 주입합니다. 이를 통해 AI 서버 없이도 호출 횟수·인용 불일치·단위 오류·시간 초과·입력 보존을 재현할 수 있습니다.

[test_extraction_agent.py](../tests/test_extraction_agent.py#L85)는 2회 호출, 검색 결과의 다음 요청 전달, 원문과 메타데이터 보존을 확인합니다. 자동검사 통과는 해당 코드 조건의 검증이며 모델의 현업 정확도는 아닙니다.

## 발표에서 설명할 핵심

“AI는 원문에서 근거와 값을 제안합니다. 코드가 인용·값·단위를 검증하고, 담당자가 확인한 변경 관계로 기준을 만든 뒤 사양을 비교합니다. 근거가 부족하면 보류하며, 최종 설계 판단은 담당자가 수행합니다.”

현재처럼 문서 형식을 제한하면 규칙만으로 처리할 수 있는 범위도 있습니다. 2회 모델 호출 구조가 단일 호출이나 규칙 방식보다 유리하다는 결과는 아직 없으며, 같은 문서로 정확도와 지연을 비교하는 후속 실험이 필요합니다.
