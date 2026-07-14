"""会计期间结账与报表默认日期区间。"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def last_month_range(today: date) -> tuple[date, date]:
    """上月 1 日 ~ 上月最后一天。"""
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev, last_prev


def month_end(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def next_month_end(after: date) -> date:
    if after.month == 12:
        first = date(after.year + 1, 1, 1)
    else:
        first = date(after.year, after.month + 1, 1)
    return month_end(first)


def previous_month_end(current: date) -> date:
    first = current.replace(day=1)
    return first - timedelta(days=1)


def period_edit_block_reason(company, doc_date: date | None) -> str | None:
    """已结账期间内的业务不可改删。可改返回 None。"""
    closed = getattr(company, "period_closed_through", None)
    if not doc_date or not closed:
        return None
    if doc_date <= closed:
        return f"该业务日期在已结账期间（已结至 {closed:%Y-%m-%d}），不可修改/删除"
    return None


def report_date_range(company, today: date, req_from, req_to) -> tuple[date, date]:
    """解析 ?from/?to；未指定时：未结上月底则默认上月整月，否则默认本月 1 日~今天。"""
    rf = _parse_date(req_from)
    rt = _parse_date(req_to)
    if rf or rt:
        return rf or today.replace(day=1), rt or today
    lm_first, lm_last = last_month_range(today)
    closed = getattr(company, "period_closed_through", None) if company else None
    if closed and closed >= lm_last:
        return today.replace(day=1), today
    return lm_first, lm_last


def report_date_range_overview(today: date, req_from, req_to) -> tuple[date, date]:
    """跨公司总览：未指定日期时默认上月整月。"""
    rf = _parse_date(req_from)
    rt = _parse_date(req_to)
    if rf or rt:
        return rf or today.replace(day=1), rt or today
    return last_month_range(today)


def suggested_close_through(company, today: date) -> date | None:
    """建议本次结账截止日（顺序月结，不超过上月底）。"""
    lm_last = last_month_range(today)[1]
    closed = company.period_closed_through
    if not closed:
        return lm_last
    nxt = next_month_end(closed)
    return nxt if nxt <= lm_last else None


def close_period(company, close_through: date, *, today: date | None = None) -> None:
    """结账：锁定 close_through 及之前所有业务日期。"""
    close_through = month_end(close_through)
    today = today or date.today()
    lm_last = last_month_range(today)[1]
    if close_through > lm_last:
        raise ValueError(f"不能结账未来期间，当前最多可结至 {lm_last:%Y-%m-%d}")
    if company.period_closed_through and close_through <= company.period_closed_through:
        raise ValueError(f"该期间已结账（已结至 {company.period_closed_through:%Y-%m-%d}）")
    if company.period_closed_through:
        expected = next_month_end(company.period_closed_through)
        if close_through != expected:
            raise ValueError(f"请按顺序结账：下一期间应结至 {expected:%Y-%m-%d}")
    company.period_closed_through = close_through
    company.save(update_fields=["period_closed_through"])


def unclose_period(company) -> None:
    """反结账：回退一个已结期间（回退至上一月末；若已回到启用月之前则置空）。"""
    from django.conf import settings

    if not company.period_closed_through:
        raise ValueError("当前无已结账期间")
    prev = previous_month_end(company.period_closed_through.replace(day=1))
    opening_month_start = settings.OPENING_DATE.replace(day=1)
    if prev < opening_month_start:
        company.period_closed_through = None
    else:
        company.period_closed_through = prev
    company.save(update_fields=["period_closed_through"])


def get_report_dates(request, company=None):
    """视图层取报表默认区间（配合 request.GET from/to）。"""
    from django.utils import timezone
    today = timezone.localdate()
    if company is None:
        return report_date_range_overview(today, request.GET.get("from"), request.GET.get("to"))
    return report_date_range(company, today, request.GET.get("from"), request.GET.get("to"))
