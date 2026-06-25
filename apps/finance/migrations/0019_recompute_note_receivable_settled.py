"""口径修正：应收票据「已用」只算背书/托收（票出去），核销应收账款不再消耗票面。

收到客户票据抵货款是 借应收票据/贷应收账款——票为持有资产，不该被「核销应收」消耗。
历史上 settle_receivable_against_sales 把核销应收也计入 settled_amount，使持有的票从账上消失。
本迁移把每张应收票据的 settled_amount 重算为「仅背书(is_endorsement=True)合计」，
并据此重置状态（未用>0→在手；用完→已背书）。应付票据不动（其抵应付本就是票出去）。
发票侧的已核销额（应收账款减少）不变——核销应收记录保留，仍正确冲减应收账款。
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import Sum


def recompute(apps, schema_editor):
    NoteReceivable = apps.get_model("finance", "NoteReceivable")
    NoteSettlement = apps.get_model("finance", "NoteSettlement")
    ZERO = Decimal("0.00")
    for n in NoteReceivable.objects.all():
        if n.status == "void":
            continue
        endorsed = (NoteSettlement.objects.filter(
            company_id=n.company_id, note_kind="ar_note", note_id=n.pk,
            is_endorsement=True).aggregate(s=Sum("amount"))["s"] or ZERO)
        unused = n.amount - endorsed
        new_status = "endorsed" if unused <= 0 and endorsed > 0 else "on_hand"
        if n.settled_amount != endorsed or n.status != new_status:
            n.settled_amount = endorsed
            n.status = new_status
            n.save(update_fields=["settled_amount", "status"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("finance", "0018_expenserecord")]
    operations = [migrations.RunPython(recompute, noop)]
