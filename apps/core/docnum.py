"""单据编号生成：{前缀}-{公司编号}-{yyyymmdd}-{当日序号3位}。

如 RK-C1-20260605-001。序号零填充，按 doc_no 倒序即可取当日最大值。
低并发场景（5 用户）+ 请求级事务下足够安全。
"""


def next_doc_no(model, company, prefix, doc_date) -> str:
    base = f"{prefix}-{company.code}-{doc_date:%Y%m%d}-"
    last = (
        model.objects.filter(company=company, doc_no__startswith=base)
        .order_by("-doc_no")
        .first()
    )
    seq = int(last.doc_no[len(base):]) + 1 if last else 1
    return f"{base}{seq:03d}"
