"""通用「公司维度」增删改基类，供各业务模块复用（masterdata/finance/…）。

子类声明 model / form_class / title / columns / *_url_name，即得到：
- 列表：按 columns 自动渲染、按权限显隐增删改按钮；
- 新增/编辑：自动绑定当前账套与创建人、写审计日志、成功提示；
- 删除：确认页 + 审计日志。
模板：templates/crud/{list,form,confirm_delete}.html（通用）。
"""

from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .mixins import CompanyScopedFormMixin, CompanyScopedMixin, FilteredListMixin
from .models import AuditLog


class ScopedListView(FilteredListMixin, CompanyScopedMixin, ListView):
    """通用列表：columns = [(表头, 字段名)]，自动取值渲染。"""

    template_name = "crud/list.html"
    context_object_name = "objects"
    columns: list = []
    title = ""
    create_url_name = ""
    update_url_name = ""
    delete_url_name = ""
    import_url_name = ""  # 可选：列表旁「导入」按钮

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = []
        for obj in ctx["objects"]:
            cells = []
            for _, attr in self.columns:
                value = getattr(obj, attr)
                if callable(value):
                    value = value()
                if isinstance(value, bool):
                    value = "是" if value else "否"
                cells.append(value)
            rows.append({
                "cells": cells,
                "edit_url": reverse_lazy(self.update_url_name, args=[obj.pk]),
                "delete_url": reverse_lazy(self.delete_url_name, args=[obj.pk]),
            })
        meta = self.model._meta
        user = self.request.user
        ctx.update({
            "title": self.title,
            "headers": [h for h, _ in self.columns],
            "rows": rows,
            "create_url": reverse_lazy(self.create_url_name),
            "import_url": (reverse_lazy(self.import_url_name) if self.import_url_name else ""),
            "can_add": user.has_perm(f"{meta.app_label}.add_{meta.model_name}"),
            "can_change": user.has_perm(f"{meta.app_label}.change_{meta.model_name}"),
            "can_delete": user.has_perm(f"{meta.app_label}.delete_{meta.model_name}"),
        })
        return ctx


class ScopedCreateView(CompanyScopedFormMixin, CreateView):
    template_name = "crud/form.html"
    perm_action = "add"
    title = ""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"新增{self.title}"
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.record(
            actor=self.request.user, company=self.object.company,
            action=AuditLog.Action.CREATE, target=self.object,
            summary=f"新增{self.title} {self.object}",
        )
        messages.success(self.request, f"已新增{self.title}：{self.object}")
        return response


class ScopedUpdateView(CompanyScopedFormMixin, UpdateView):
    template_name = "crud/form.html"
    perm_action = "change"
    title = ""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"编辑{self.title}"
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.record(
            actor=self.request.user, company=self.object.company,
            action=AuditLog.Action.UPDATE, target=self.object,
            summary=f"修改{self.title} {self.object}",
        )
        messages.success(self.request, f"已保存{self.title}：{self.object}")
        return response


class ScopedDeleteView(CompanyScopedMixin, DeleteView):
    template_name = "crud/confirm_delete.html"
    perm_action = "delete"
    title = ""

    def form_valid(self, form):
        obj = self.get_object()
        AuditLog.record(
            actor=self.request.user, company=obj.company,
            action=AuditLog.Action.DELETE, target=obj,
            summary=f"删除{self.title} {obj}",
        )
        messages.success(self.request, f"已删除{self.title}：{obj}")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = self.title
        return ctx
