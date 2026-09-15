"""Exercise actual wheel callbacks without creating Tk windows."""
import unittest
from types import SimpleNamespace
from unittest import mock

from app.gui.main_gateway import GatewayApp


class MousewheelEventTests(unittest.TestCase):
    def check_events(self, callback, canvas):
        cases = [
            (SimpleNamespace(delta=-120, num="??"), 1),
            (SimpleNamespace(delta=240, num="??"), -2),
            (SimpleNamespace(delta=-1, num="??"), 1),
            (SimpleNamespace(delta="??", num=4), -1),
            (SimpleNamespace(delta="??", num="5"), 1),
            (SimpleNamespace(delta="??", num="??"), None),
            (SimpleNamespace(delta=None, num=None), None),
            (SimpleNamespace(), None),
        ]
        for event, expected in cases:
            with self.subTest(event=vars(event)):
                canvas.yview_scroll.reset_mock()
                result = callback(event)
                if expected is None:
                    self.assertIsNone(result)
                    canvas.yview_scroll.assert_not_called()
                else:
                    self.assertEqual(result, "break")
                    canvas.yview_scroll.assert_called_once_with(expected, "units")

    def test_workflow_list_wheel_handles_tk_unavailable_fields(self):
        canvas = mock.Mock()
        app = SimpleNamespace(_workflow_canvas=canvas)
        self.check_events(lambda event: GatewayApp._on_workflow_mousewheel(app, event), canvas)
        app._workflow_canvas = None
        self.assertIsNone(GatewayApp._on_workflow_mousewheel(app, SimpleNamespace(delta=120)))

    def test_model_download_wheel_handles_tk_unavailable_fields(self):
        canvas, widget = mock.Mock(), mock.Mock()
        canvas.winfo_children.return_value = []
        widget.winfo_children.return_value = []
        GatewayApp._bind_model_download_mousewheel_tree(SimpleNamespace(), widget, canvas)
        callback = widget.bind.call_args_list[0].args[1]
        self.check_events(callback, canvas)


if __name__ == "__main__":
    unittest.main()
