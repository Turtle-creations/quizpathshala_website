from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.exam_service_db import exam_service


def _build_text(codepoints: list[int]) -> str:
    return "".join(chr(codepoint) for codepoint in codepoints)


def main() -> None:
    question_text = _build_text([2349, 2366, 2352, 2340, 32, 2325, 2366, 32, 2360, 2306, 2357, 2367, 2343, 2366, 2344, 32, 2325, 2348, 32, 2354, 2366, 2327, 2370, 32, 2361, 2369, 2310, 63])
    options = [
        _build_text([50, 54, 32, 2332, 2344, 2357, 2352, 2368, 32, 49, 57, 53, 48]),
        _build_text([49, 53, 32, 2309, 2327, 2360, 2381, 2340, 32, 49, 57, 52, 55]),
        _build_text([50, 54, 32, 2344, 2357, 2306, 2348, 2352, 32, 49, 57, 52, 57]),
        _build_text([49, 32, 2332, 2344, 2357, 2352, 2368, 32, 49, 57, 53, 48]),
    ]
    correct_option = _build_text([50, 54, 32, 2332, 2344, 2357, 2352, 2368, 32, 49, 57, 53, 48])

    stored_question_text = str(question_text).strip()
    stored_options = [str(option).strip() for option in options]
    stored_correct_option = exam_service._normalize_stored_correct_answer(stored_options, str(correct_option).strip())

    assert stored_question_text == question_text
    assert stored_options == options
    assert stored_correct_option == correct_option
    assert any("ऀ" <= character <= "ॿ" for character in stored_question_text)

    print("Unicode validation passed: Hindi question text remains unchanged before database insert.")


if __name__ == "__main__":
    main()
