import json
import unittest

from desktop_geometry import ScreenGeometry
from screen_vision import AgentDecision, ScreenFrame, suggest_cursor_action


class ActionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = ScreenFrame(
            png_bytes=b"png",
            width=1366,
            height=768,
            geometry=ScreenGeometry(0, 0, 1366, 768),
            captured_at=0.0,
        )

    def parse(self, payload: object):
        response = json.dumps(payload)
        return suggest_cursor_action(
            lambda content: response,
            "gemini-test",
            "prueba",
            self.frame,
        )

    def test_parses_the_shared_action_types(self) -> None:
        cases = (
            ({"type": "move", "x": 20, "y": 30}, "move"),
            ({"type": "click", "x": 20, "y": 30}, "click"),
            ({"type": "double_click", "x": 20, "y": 30}, "double_click"),
            (
                {
                    "type": "drag",
                    "start_x": 20,
                    "start_y": 30,
                    "end_x": 40,
                    "end_y": 50,
                },
                "drag",
            ),
            ({"type": "write", "text": "Hola"}, "write"),
            ({"type": "press", "key": "ENTER"}, "press"),
            (
                {"type": "press", "keys": ["WINDOWS", "S"]},
                "press",
            ),
            ({"type": "scroll", "x": 20, "y": 30, "delta": 120}, "scroll"),
            ({"type": "wait", "seconds": 1}, "wait"),
        )
        for raw_action, expected in cases:
            with self.subTest(action=expected):
                result = self.parse({"status": "continue", "actions": [raw_action]})
                self.assertEqual(result.status, "continue")
                self.assertEqual(len(result.actions), 1)
                self.assertEqual(result.actions[0].action, expected)

    def test_parses_terminal_success_and_error_without_actions(self) -> None:
        for status in ("success", "error"):
            with self.subTest(status=status):
                result = self.parse({"status": status, "message": "estado observado"})
                self.assertEqual(result, AgentDecision(status, message="estado observado"))

    def test_rejects_malformed_or_out_of_bounds_batch_before_execution(self) -> None:
        invalid_batches = (
            {
                "actions": [
                    {"type": "move", "x": 10, "y": 20},
                    {"type": "click", "x": 1366, "y": 20},
                ],
                "status": "continue",
            },
            {"status": "continue", "actions": [{"type": "click", "x": True, "y": 20}]},
            {"status": "continue", "actions": [{"type": "click", "x": 10, "y": 20, "command": "x"}]},
            {"status": "continue", "actions": [{"type": "wait", "seconds": 100}]},
            {"status": "continue", "actions": [{"type": "write", "text": "mi contraseña secreta"}]},
            {
                "status": "continue",
                "actions": [{"type": "error", "message": "no ejecutable"}],
            },
            {"status": "continue", "actions": [{"type": "click", "x": 10, "y": 20}] * 5},
            {"status": "continue", "actions": []},
            {"status": "success", "message": "ok", "actions": []},
            {"status": "error", "message": "fallo", "actions": [{"type": "wait", "seconds": 1}]},
            {"status": "success", "message": ""},
            {"status": "error", "message": "fallo", "extra": True},
            {"status": "unknown", "message": "x"},
            {
                "status": "continue",
                "actions": [{"type": "click", "x": 10, "y": 20}],
                "approved": True,
            },
        )
        for payload in invalid_batches:
            with self.subTest(payload=payload), self.assertRaises((ValueError, RuntimeError)):
                self.parse(payload)


if __name__ == "__main__":
    unittest.main()
