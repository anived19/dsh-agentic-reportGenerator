class TavilyBudget:
    """Standalone rate limit tracker for Tavily calls across the agent run."""
    def __init__(self, budget: int = 5):
        self.budget = budget
        self.calls = 0

    def increment(self) -> bool:
        """Increments calls and returns True if under budget, False otherwise."""
        if self.calls >= self.budget:
            return False
        self.calls += 1
        return True

    def check(self) -> bool:
        """Returns True if under budget, False otherwise without incrementing."""
        return self.calls < self.budget

tavily_budget = TavilyBudget(5)
