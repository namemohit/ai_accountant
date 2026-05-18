from abc import ABC, abstractmethod
from typing import Dict, Any, List

class AccountingProvider(ABC):
    @abstractmethod
    def create_voucher(self, data: Dict[str, Any]) -> str:
        """Create a voucher entry in the accounting system."""
        pass

    @abstractmethod
    def get_ledgers(self) -> List[str]:
        """Fetch list of all ledgers."""
        pass

    @abstractmethod
    def get_balance(self, ledger_name: str) -> float:
        """Get balance for a specific ledger."""
        pass
