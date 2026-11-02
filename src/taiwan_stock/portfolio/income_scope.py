"""Mutually exclusive income-calendar scopes and receiving-bank lookup."""

from collections import Counter
from dataclasses import dataclass

from taiwan_stock.catalog.bank_dictionary import canonical_name
from taiwan_stock.infrastructure.db import read_transaction


@dataclass(frozen=True)
class Scope:
    value: str
    label: str
    kind: str = "all"
    key: str | None = None

    @property
    def description(self):
        suffix = {"account": "含各領息銀行", "bank": "含各證券帳戶"}
        return (
            f"{self.label} · {suffix[self.kind]}" if self.kind != "all" else self.label
        )

    def includes(self, account_id, bank):
        if self.kind == "account":
            return account_id == self.key
        if self.kind == "bank":
            return canonical_name(bank) == self.key
        return True


class ScopeOptions:
    """Load banks once; choices remain stable across empty payment months."""

    def __init__(self, conn):
        with read_transaction(conn):
            accounts = conn.execute(
                """SELECT b.* FROM broker_accounts b
                   WHERE b.status = 'active'
                      OR EXISTS (SELECT 1 FROM live_trades t
                                 WHERE t.broker_account_id = b.id)
                      OR EXISTS (SELECT 1 FROM dividend_receipts r
                                 WHERE r.broker_account_id = b.id)
                   ORDER BY b.name, b.id"""
            ).fetchall()
            self.defaults = {a["id"]: a["dividend_bank_name"] for a in accounts}
            self.preferences = {
                (r["broker_account_id"], r["security_id"]): r["bank_name"]
                for r in conn.execute("SELECT * FROM dividend_bank_preferences")
            }
            names = Counter(a["name"] for a in accounts)
            self.accounts = []
            for a in accounts:
                label = a["name"]
                if names[label] > 1:
                    label += f"（{a['id'][:8]}）"
                if a["status"] == "disabled":
                    label += "（已停用）"
                self.accounts.append(
                    Scope(f"account:{a['id']}", f"證券｜{label}", "account", a["id"])
                )
            banks = {canonical_name(bank) for bank in self.defaults.values()}
            banks.update(
                canonical_name(bank)
                for (account_id, _), bank in self.preferences.items()
                if account_id in self.defaults
            )
            self.banks = [
                Scope(f"bank:{bank}", f"銀行｜{bank}", "bank", bank)
                for bank in sorted(b for b in banks if b is not None)
            ]
            if None in banks:
                self.banks.append(Scope("bank-unset", "銀行｜未設定", "bank"))
            self.all = Scope("all", "全部")

    def bank_for(self, account_id, security_id):
        # Preserve the API's original bank text; normalize only when matching.
        return self.preferences.get(
            (account_id, security_id), self.defaults.get(account_id)
        )

    def select(self, value):
        if value.startswith("bank:"):
            bank = canonical_name(value.partition(":")[2])
            if bank is None:
                raise ValueError("請選擇領息銀行")
            value = f"bank:{bank}"
        for option in [self.all, *self.accounts, *self.banks]:
            if option.value == value:
                return option
        raise ValueError("查看範圍無效，請重新選擇證券帳戶或領息銀行")


def api_filter(*, scope="all", account_id=None, bank=None, bank_unset=False):
    """Translate the API's explicit fields without accepting an intersection."""
    if scope == "all" and not (account_id or bank or bank_unset):
        return "all"
    if scope == "account" and account_id and not (bank or bank_unset):
        return f"account:{account_id}"
    if scope == "bank" and not account_id:
        if bank_unset and not bank:
            return "bank-unset"
        if bank and not bank_unset:
            return f"bank:{bank}"
    raise ValueError("請選擇單一查看範圍：全部、證券帳戶或領息銀行")
