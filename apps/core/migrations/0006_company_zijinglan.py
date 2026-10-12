"""增加第四家关联公司：C4 紫荆蓝，并在各账套挂上对方往来单位。

已有名称含「紫荆蓝」的往来单位只补「对应关联企业」，不新建重复档案。
"""

from django.db import migrations


C4 = {
    "code": "C4",
    "name": "紫荆蓝新材料科技（上海）有限公司",
    "short_name": "紫荆蓝",
    "full_name": "紫荆蓝新材料科技（上海）有限公司",
}

OTHERS = (
    ("C1", "安博诺新材料科技（上海）有限公司", "安博诺"),
    ("C2", "上海恒本源化学有限公司", "恒本源"),
    ("C3", "鸿威达新材料科技（上海）有限公司", "鸿威达"),
)


def _next_partner_code(BusinessPartner, company):
    used = set()
    for code in BusinessPartner.objects.filter(company=company).values_list("code", flat=True):
        if code.startswith("W") and code[1:].isdigit():
            used.add(int(code[1:]))
    n = 1
    while n in used:
        n += 1
    return f"W{n:04d}"


def _ensure_partner(BusinessPartner, company, *, name, related, match_keys):
    existing = []
    for key in match_keys:
        existing = list(BusinessPartner.objects.filter(company=company, name__contains=key))
        if existing:
            break
    if existing:
        for partner in existing:
            partner.related_company = related
            partner.is_customer = True
            partner.is_supplier = True
            partner.name = name
            partner.save(update_fields=["related_company", "is_customer", "is_supplier", "name"])
        return
    BusinessPartner.objects.create(
        company=company,
        code=_next_partner_code(BusinessPartner, company),
        name=name,
        related_company=related,
        is_customer=True,
        is_supplier=True,
    )


def add_zijinglan(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    BusinessPartner = apps.get_model("masterdata", "BusinessPartner")

    c4, _created = Company.objects.get_or_create(
        code=C4["code"],
        defaults={
            "name": C4["name"],
            "short_name": C4["short_name"],
            "full_name": C4["full_name"],
            "is_related": True,
            "is_active": True,
        },
    )
    if not c4.full_name:
        c4.full_name = C4["full_name"]
        c4.short_name = c4.short_name or C4["short_name"]
        c4.is_related = True
        c4.is_active = True
        c4.save(update_fields=["full_name", "short_name", "is_related", "is_active"])

    for code, name, short in OTHERS:
        other = Company.objects.filter(code=code).first()
        if other is None:
            continue
        _ensure_partner(
            BusinessPartner, other, name=C4["name"], related=c4, match_keys=("紫荆蓝",))
        _ensure_partner(
            BusinessPartner, c4, name=name, related=other, match_keys=(short,))


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_company_period_closed_through"),
        ("masterdata", "0006_partner_fk_and_proxies"),
    ]

    operations = [
        migrations.RunPython(add_zijinglan, migrations.RunPython.noop),
    ]
