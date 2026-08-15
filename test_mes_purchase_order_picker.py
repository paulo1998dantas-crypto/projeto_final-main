import unittest
from pathlib import Path


class MesPurchaseOrderPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).with_name("templates") / "gestao_os.html"
        ).read_text(encoding="utf-8")

    def test_picker_opens_and_lists_available_orders_without_a_query(self):
        self.assertIn('role="combobox"', self.template)
        self.assertIn('role="listbox"', self.template)
        self.assertIn('onfocus="openPurchaseOrderPicker()"', self.template)
        self.assertIn("openPurchaseOrderPicker", self.template)
        self.assertIn("&limit=50", self.template)

    def test_picker_filters_with_debounce_and_ignores_stale_responses(self):
        self.assertIn('oninput="queuePurchaseOrderSearch()"', self.template)
        self.assertIn("setTimeout(searchPurchaseOrders,250)", self.template)
        self.assertIn("request!==purchasePickerState.request", self.template)

    def test_picker_supports_keyboard_navigation_and_explicit_linking(self):
        self.assertIn('onkeydown="handlePurchaseOrderPickerKey(event)"', self.template)
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(f"event.key==='{key}'", self.template)
        self.assertIn("!picker.contains(event.target)", self.template)
        self.assertIn('class="purchase-option"', self.template)
        self.assertIn("linkPurchaseOrder('${x.id}')", self.template)


if __name__ == "__main__":
    unittest.main()
