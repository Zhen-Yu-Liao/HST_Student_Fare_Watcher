import unittest
from unittest.mock import patch

from thsrc_auto_booker import (
    SelectedTrain,
    extract_booking_code,
    has_confirmed_booking,
    is_sold_out_error,
    is_retryable_round_failure,
    validate_booking_config,
    wait_for_booking_confirmation,
)


class FakeLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self, timeout=0):
        return False


class FakePage:
    def __init__(self, body_text):
        self.body_text = body_text

    def evaluate(self, script):
        return self.body_text

    def locator(self, selector):
        return FakeLocator()


class BookingConfirmationTests(unittest.TestCase):
    def test_extracts_eight_digit_booking_code_after_label(self):
        text = "完成訂位\n訂位代號：1234 5678\n新竹 → 台南"

        self.assertEqual(extract_booking_code(text), "12345678")

    def test_does_not_treat_generic_booking_code_text_as_success(self):
        text = "請輸入訂位代號共8碼，或使用忘記訂位代號查詢。"

        self.assertIsNone(extract_booking_code(text))
        self.assertFalse(has_confirmed_booking(text))

    def test_requires_booking_code_even_when_completion_words_exist(self):
        text = "已完成資料送出，但未顯示訂位代號。"

        self.assertFalse(has_confirmed_booking(text))

    def test_wait_for_booking_confirmation_returns_evidence(self):
        page = FakePage("完成訂位 訂位代號：87654321")
        selected = SelectedTrain(train_no="817", discount_text="學生5折", row_text="817 學生5折")

        with patch(
            "thsrc_auto_booker.save_page_artifacts",
            return_value={"html": "success.html", "text": "success.txt", "screenshot": "success.png"},
        ):
            evidence = wait_for_booking_confirmation(page, selected, timeout_ms=100)

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.booking_code, "87654321")
        self.assertEqual(evidence.train_no, "817")

    def test_validate_booking_config_blocks_missing_idno(self):
        config = {"booking": {"idno": "", "phone": "", "email": ""}}

        critical, optional = validate_booking_config(config)

        self.assertEqual(critical, ["THSRC_IDNO"])
        self.assertEqual(optional, ["THSRC_PHONE", "THSRC_EMAIL"])

    def test_validate_booking_config_accepts_required_idno(self):
        config = {"booking": {"idno": "A123456789", "phone": "", "email": ""}}

        critical, optional = validate_booking_config(config)

        self.assertEqual(critical, [])
        self.assertEqual(optional, ["THSRC_PHONE", "THSRC_EMAIL"])

    def test_retryable_round_failure_reasons(self):
        self.assertTrue(is_retryable_round_failure("captcha_failed"))
        self.assertTrue(is_retryable_round_failure("submit_failed"))
        self.assertTrue(is_retryable_round_failure("round_exception"))
        self.assertTrue(is_retryable_round_failure("sold_out"))
        self.assertFalse(is_retryable_round_failure("confirmation_missing"))
        self.assertFalse(is_retryable_round_failure("exception"))

    def test_detects_sold_out_error(self):
        text = "您選擇的車次可銷售之座位或優惠座位已額滿，請重新操作。"

        self.assertTrue(is_sold_out_error(text))


if __name__ == "__main__":
    unittest.main()
