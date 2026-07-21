"""清理历史「改前*」修改冲正流水及其对应原流水，并重算结存快照。"""

from django.core.management.base import BaseCommand

from apps.inventory.models import StockMove
from apps.inventory.rebalance import rebalance_product


def _find_reversed_original(rev: StockMove):
    """定位「改前」冲正流水所对应的原流水。"""
    orig_dir = StockMove.Direction.IN if rev.direction == StockMove.Direction.OUT else StockMove.Direction.OUT
    base = (StockMove.objects
            .filter(company=rev.company, product=rev.product, source_id=rev.source_id,
                    quantity=rev.quantity, amount=rev.amount, direction=orig_dir,
                    created_at__lt=rev.created_at)
            .exclude(source_no__startswith="改前")
            .exclude(source_no__startswith="作废"))
    orig = base.order_by("-created_at", "-id").first()
    if orig:
        return orig
    return (StockMove.objects
            .filter(company=rev.company, product=rev.product,
                    quantity=rev.quantity, amount=rev.amount, direction=orig_dir,
                    created_at__lt=rev.created_at)
            .exclude(source_no__startswith="改前")
            .exclude(source_no__startswith="作废")
            .order_by("-created_at", "-id")
            .first())


class Command(BaseCommand):
    help = "删除台账中「改前*」修改冲正流水及被其冲销的原流水，并重算受影响商品结存。"

    def add_arguments(self, parser):
        parser.add_argument("--company", help="仅处理指定公司 code，如 C3")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")

    def handle(self, *args, **options):
        qs = StockMove.objects.filter(source_no__startswith="改前").order_by("created_at", "id")
        if code := options.get("company"):
            qs = qs.filter(company__code=code)

        affected = set()
        removed = 0
        for rev in qs:
            orig = _find_reversed_original(rev)
            if not orig:
                self.stdout.write(self.style.WARNING(
                    f"跳过：未找到原流水 {rev.source_no} id={rev.pk}"))
                continue
            affected.add((rev.company_id, rev.product_id))
            if options["dry_run"]:
                removed += 2
                continue
            orig.delete()
            rev.delete()
            removed += 2

        if options["dry_run"]:
            self.stdout.write(self.style.NOTICE(f"[dry-run] 将删除 {removed} 条流水，影响 {len(affected)} 个商品"))
            return

        for company_id, product_id in affected:
            from apps.core.models import Company
            from apps.masterdata.models import Product
            company = Company.objects.get(pk=company_id)
            product = Product.objects.get(pk=product_id)
            rebalance_product(company, product)

        self.stdout.write(self.style.SUCCESS(
            f"已删除 {removed} 条流水，重算 {len(affected)} 个商品结存"))
