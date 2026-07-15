"""M19：建立往来单位表，并从客户/供应商灌数（保留客户主键；供应商映射后改写 FK）。"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _copy_partners(apps, schema_editor):
    Customer = apps.get_model("masterdata", "Customer")
    Supplier = apps.get_model("masterdata", "Supplier")
    BusinessPartner = apps.get_model("masterdata", "BusinessPartner")
    db = schema_editor.connection.alias

    # 1) 客户：保留主键 → 业务单据 customer_id 无需改写
    for c in Customer.objects.using(db).all():
        BusinessPartner.objects.using(db).create(
            pk=c.pk,
            company_id=c.company_id,
            created_by_id=c.created_by_id,
            created_at=c.created_at,
            updated_at=c.updated_at,
            code=c.code,
            name=c.name,
            contact=c.contact,
            phone=c.phone,
            tax_no=c.tax_no,
            address=c.address,
            related_company_id=c.related_company_id,
            is_customer=True,
            is_supplier=False,
            is_active=c.is_active,
            remark=c.remark or "",
        )

    # 2) 供应商：合并到已有往来单位或新建；记录 id 映射
    sup_map = {}  # old_supplier_id -> businesspartner_id
    for s in Supplier.objects.using(db).all():
        bp = None
        if s.related_company_id:
            bp = (BusinessPartner.objects.using(db)
                  .filter(company_id=s.company_id, related_company_id=s.related_company_id)
                  .first())
        if bp is None and (s.tax_no or "").strip():
            bp = (BusinessPartner.objects.using(db)
                  .filter(company_id=s.company_id, tax_no=s.tax_no)
                  .first())
        if bp is None:
            bp = (BusinessPartner.objects.using(db)
                  .filter(company_id=s.company_id, code=s.code)
                  .first())
        if bp is None:
            bp = (BusinessPartner.objects.using(db)
                  .filter(company_id=s.company_id, name=s.name)
                  .first())

        if bp is not None:
            bp.is_supplier = True
            if not bp.contact and s.contact:
                bp.contact = s.contact
            if not bp.phone and s.phone:
                bp.phone = s.phone
            if not bp.tax_no and s.tax_no:
                bp.tax_no = s.tax_no
            if not bp.address and s.address:
                bp.address = s.address
            if not bp.related_company_id and s.related_company_id:
                bp.related_company_id = s.related_company_id
            note = f"合并供应商码 {s.code}"
            if note not in (bp.remark or ""):
                bp.remark = ((bp.remark + "；") if bp.remark else "") + note
            bp.save()
            sup_map[s.pk] = bp.pk
        else:
            # 新码：若与客户编码冲突加后缀
            code = s.code
            if BusinessPartner.objects.using(db).filter(company_id=s.company_id, code=code).exists():
                code = f"{s.code}-S"
            bp = BusinessPartner.objects.using(db).create(
                company_id=s.company_id,
                created_by_id=s.created_by_id,
                created_at=s.created_at,
                updated_at=s.updated_at,
                code=code,
                name=s.name,
                contact=s.contact,
                phone=s.phone,
                tax_no=s.tax_no,
                address=s.address,
                related_company_id=s.related_company_id,
                is_customer=False,
                is_supplier=True,
                is_active=s.is_active,
                remark=s.remark or "",
            )
            sup_map[s.pk] = bp.pk

    # 3) 改写指向供应商的 FK
    ModelSpecs = [
        ("purchasing", "PurchaseInbound"),
        ("purchasing", "PurchaseOrder"),
        ("finance", "PurchaseInvoice"),
        ("finance", "Payment"),
        ("finance", "NotePayable"),
        ("finance", "PartnerOffset"),
    ]
    for app_label, model_name in ModelSpecs:
        try:
            Model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        if not any(f.name == "supplier" for f in Model._meta.fields):
            continue
        for old_id, new_id in list(sup_map.items()):
            if old_id == new_id:
                continue
            (Model.objects.using(db)
             .filter(supplier_id=old_id)
             .update(supplier_id=new_id))


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0005_company_period_closed_through"),
        ("masterdata", "0004_alter_invoicequota_options_and_more"),
        # 保证业务表已存在，便于改写 supplier_id
        ("purchasing", "0007_sale_purchase_return_types"),
        ("sales", "0008_sale_purchase_return_types"),
        ("finance", "0024_purchase_order_m18"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusinessPartner",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("code", models.CharField(max_length=32, verbose_name="编码")),
                ("name", models.CharField(max_length=128, verbose_name="名称")),
                ("contact", models.CharField(blank=True, max_length=64, verbose_name="联系人")),
                ("phone", models.CharField(blank=True, max_length=32, verbose_name="电话")),
                ("tax_no", models.CharField(blank=True, max_length=32, verbose_name="税号")),
                ("address", models.CharField(blank=True, max_length=255, verbose_name="地址")),
                ("is_customer", models.BooleanField(default=False, verbose_name="客户")),
                ("is_supplier", models.BooleanField(default=False, verbose_name="供应商")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("remark", models.CharField(blank=True, max_length=255, verbose_name="备注")),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="%(class)s_set", to="core.company", verbose_name="所属公司")),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL, verbose_name="创建人")),
                ("related_company", models.ForeignKey(
                    blank=True, help_text="当对方是系统内关联企业（C1/C2/C3）时选择；用于关联交易自动联动。",
                    null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to="core.company", verbose_name="对应关联企业")),
            ],
            options={
                "verbose_name": "往来单位",
                "verbose_name_plural": "往来单位",
                "ordering": ["company", "code"],
            },
        ),
        migrations.AddConstraint(
            model_name="businesspartner",
            constraint=models.UniqueConstraint(fields=("company", "code"), name="uniq_partner_company_code"),
        ),
        migrations.AddConstraint(
            model_name="businesspartner",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_customer", True), ("is_supplier", True), _connector="OR"),
                name="partner_must_have_role",
            ),
        ),
        migrations.RunPython(_copy_partners, _noop),
    ]
