"""修改后不留「改前」流水、结存重算与历史清理。"""

from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockMove
from apps.inventory.rebalance import normalize_balance_qty_amount
from apps.inventory.services import post_inbound, post_outbound, reverse_move
from apps.masterdata.models import Product


class EditCleanupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def test_normalize_zero_qty_clears_amount(self):
        qty, amt = normalize_balance_qty_amount(Decimal("0.000"), Decimal("451327.43"))
        self.assertEqual(qty, Decimal("0.000"))
        self.assertEqual(amt, Decimal("0.00"))

    def test_cleanup_command_removes_edit_pairs(self):
        from django.core.management import call_command
        from apps.inventory.models import StockBalance
        mv = post_inbound(self.c1, self.p, Decimal("10"), Decimal("5"))
        rev = reverse_move(mv, source_type="PurchaseInboundEdit",
                           source_id="1", source_no="改前RK-TEST")
        self.assertEqual(StockMove.objects.count(), 2)
        call_command("cleanup_edit_reversals")
        self.assertEqual(StockMove.objects.count(), 0)
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("0.000"))
        self.assertEqual(bal.amount, Decimal("0.00"))

    def test_subsequent_moves_rebalanced_after_edit(self):
        from django.contrib.auth import get_user_model
        from apps.purchasing.services import create_and_post_inbound, update_and_repost_inbound
        from django.utils import timezone
        today = timezone.localdate()
        u = get_user_model().objects.create_user(username="op", password="x")
        doc = create_and_post_inbound(company=self.c1, user=u, doc_date=today,
            lines=[{"product": self.p, "quantity": Decimal("100"), "unit_price": Decimal("10")}])
        post_outbound(self.c1, self.p, Decimal("30"), date=today)
        update_and_repost_inbound(doc, user=u, doc_date=today,
            lines=[{"product": self.p, "quantity": Decimal("120"), "unit_price": Decimal("10")}])
        moves = list(StockMove.objects.filter(company=self.c1, product=self.p).order_by("created_at", "id"))
        self.assertEqual(len(moves), 2)  # 新入库 + 后续出库，无改前
        self.assertFalse(any(m.source_no.startswith("改前") for m in moves))
        last = moves[-1]
        self.assertEqual(last.balance_quantity, Decimal("90.000"))
