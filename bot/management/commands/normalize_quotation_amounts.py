from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db import transaction

from bot.models import Quotation, QuotationItem


def parse_decimal(value, default="0.00"):
    if value in (None, ""):
        return Decimal(default)
    if isinstance(value, Decimal):
        return value

    cleaned = (
        str(value)
        .strip()
        .replace("US$", "")
        .replace("USD", "")
        .replace("$", "")
        .replace(",", "")
        .replace(" ", "")
    )
    try:
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


class Command(BaseCommand):
    help = "Normalize legacy quotation/item numeric values (e.g. '$0.00') and recalculate totals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without writing to database.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        quotes_checked = 0
        quotes_changed = 0
        items_checked = 0
        items_changed = 0

        quotations = Quotation.objects.all().prefetch_related("items")

        for q in quotations:
            quotes_checked += 1
            changed = False
            item_changed_for_quote = False

            labor = parse_decimal(q.labor_cost)
            materials = parse_decimal(q.materials_cost)
            transport = parse_decimal(q.transport_cost)

            if labor != q.labor_cost:
                q.labor_cost = labor
                changed = True
            if materials != q.materials_cost:
                q.materials_cost = materials
                changed = True
            if transport != q.transport_cost:
                q.transport_cost = transport
                changed = True

            for item in q.items.all():
                items_checked += 1
                qty = parse_decimal(item.quantity, default="1.00")
                unit = parse_decimal(item.unit_price, default="0.00")
                total = qty * unit

                if qty != item.quantity or unit != item.unit_price or total != item.total_price:
                    items_changed += 1
                    item_changed_for_quote = True
                    if not dry_run:
                        QuotationItem.objects.filter(pk=item.pk).update(
                            quantity=qty,
                            unit_price=unit,
                            total_price=total,
                        )

            if changed or item_changed_for_quote:
                quotes_changed += 1
                if not dry_run:
                    # Recalculate total_amount from items + costs
                    q.save()

        if dry_run:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("Dry run complete (no DB changes committed)."))
        else:
            self.stdout.write(self.style.SUCCESS("Normalization complete."))

        self.stdout.write(
            f"Quotations checked: {quotes_checked}, quotations touched: {quotes_changed}, "
            f"items checked: {items_checked}, items fixed: {items_changed}"
        )
