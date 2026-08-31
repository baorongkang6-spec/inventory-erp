"""修正历史背书冲销业务日：误存出票日、实际登记更晚的改回登记日。

背景：背书经付款登记时，旧代码把 NoteSettlement.date 写成票据 draw_date；
8 月登记、7 月出票的票在付款一览/应付报表均显示 7 月。新代码已改取 doc_date，
本迁移按 created_at 修正仍存出票日的历史背书记录。
"""

from django.db import migrations
from django.utils import timezone


def fix_endorsement_dates(apps, schema_editor):
    NoteSettlement = apps.get_model("finance", "NoteSettlement")
    NoteReceivable = apps.get_model("finance", "NoteReceivable")
    draw_dates = dict(NoteReceivable.objects.values_list("pk", "draw_date"))
    to_update = []
    for s in NoteSettlement.objects.filter(is_endorsement=True, note_kind="ar_note"):
        stored = s.date
        if stored is None:
            continue
        reg = timezone.localtime(s.created_at).date()
        if reg <= stored:
            continue
        draw = draw_dates.get(s.note_id)
        if draw is not None and stored == draw:
            s.date = reg
            to_update.append(s)
    if to_update:
        NoteSettlement.objects.bulk_update(to_update, ["date"])


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0030_fixup_prepaid_endorsement_invoice_kind"),
    ]

    operations = [
        migrations.RunPython(fix_endorsement_dates, migrations.RunPython.noop),
    ]
