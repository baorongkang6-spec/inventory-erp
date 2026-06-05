"""商品 / 客户 / 供应商 的列表与增删改（公司维度过滤）。

三类实体结构相似，用基类统一列表渲染与成功提示，子类只声明字段差异。
"""

from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.core.mixins import CompanyScopedFormMixin, CompanyScopedMixin
from apps.core.models import AuditLog

from .forms import CustomerForm, ProductForm, SupplierForm
from .models import Customer, Product, Supplier


class MasterDataListView(CompanyScopedMixin, ListView):
    """通用列表：columns = [(表头, 字段名)]，自动取值渲染。"""

    template_name = "masterdata/list.html"
    context_object_name = "objects"
    columns: list = []
    title = ""
    create_url_name = ""
    update_url_name = ""
    delete_url_name = ""

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
            "can_add": user.has_perm(f"{meta.app_label}.add_{meta.model_name}"),
            "can_change": user.has_perm(f"{meta.app_label}.change_{meta.model_name}"),
            "can_delete": user.has_perm(f"{meta.app_label}.delete_{meta.model_name}"),
        })
        return ctx


class MasterDataCreateView(CompanyScopedFormMixin, CreateView):
    template_name = "masterdata/form.html"
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


class MasterDataUpdateView(CompanyScopedFormMixin, UpdateView):
    template_name = "masterdata/form.html"
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


class MasterDataDeleteView(CompanyScopedMixin, DeleteView):
    template_name = "masterdata/confirm_delete.html"
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


# --- 商品 ---------------------------------------------------------------------
class ProductListView(MasterDataListView):
    model = Product
    title = "商品"
    columns = [("编码", "code"), ("名称", "name"), ("规格", "spec"),
               ("单位", "unit"), ("分类", "category"), ("启用", "is_active")]
    create_url_name = "product_create"
    update_url_name = "product_update"
    delete_url_name = "product_delete"


class ProductCreateView(MasterDataCreateView):
    model = Product
    form_class = ProductForm
    title = "商品"
    success_url = reverse_lazy("product_list")


class ProductUpdateView(MasterDataUpdateView):
    model = Product
    form_class = ProductForm
    title = "商品"
    success_url = reverse_lazy("product_list")


class ProductDeleteView(MasterDataDeleteView):
    model = Product
    title = "商品"
    success_url = reverse_lazy("product_list")


# --- 客户 ---------------------------------------------------------------------
class CustomerListView(MasterDataListView):
    model = Customer
    title = "客户"
    columns = [("编码", "code"), ("名称", "name"), ("联系人", "contact"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    create_url_name = "customer_create"
    update_url_name = "customer_update"
    delete_url_name = "customer_delete"


class CustomerCreateView(MasterDataCreateView):
    model = Customer
    form_class = CustomerForm
    title = "客户"
    success_url = reverse_lazy("customer_list")


class CustomerUpdateView(MasterDataUpdateView):
    model = Customer
    form_class = CustomerForm
    title = "客户"
    success_url = reverse_lazy("customer_list")


class CustomerDeleteView(MasterDataDeleteView):
    model = Customer
    title = "客户"
    success_url = reverse_lazy("customer_list")


# --- 供应商 -------------------------------------------------------------------
class SupplierListView(MasterDataListView):
    model = Supplier
    title = "供应商"
    columns = [("编码", "code"), ("名称", "name"), ("联系人", "contact"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    create_url_name = "supplier_create"
    update_url_name = "supplier_update"
    delete_url_name = "supplier_delete"


class SupplierCreateView(MasterDataCreateView):
    model = Supplier
    form_class = SupplierForm
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


class SupplierUpdateView(MasterDataUpdateView):
    model = Supplier
    form_class = SupplierForm
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


class SupplierDeleteView(MasterDataDeleteView):
    model = Supplier
    title = "供应商"
    success_url = reverse_lazy("supplier_list")
