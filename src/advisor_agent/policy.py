"""
Decision policy for Azure Advisor recommendations.

This module encodes the rules for:
- Which findings to include/exclude (exemptions, suppressions)
- When to create, update, skip, or close tickets
- How to group findings by type
- Impact thresholds for each category
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ImpactLevel(Enum):
    """Impact severity levels."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Decision(Enum):
    """Possible decisions for each finding."""

    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"
    CLOSE = "close"


class Category(Enum):
    """Azure Advisor recommendation categories."""

    COST = "Cost"
    SECURITY = "Security"
    RELIABILITY = "Reliability"
    PERFORMANCE = "Performance"
    OPERATIONAL_EXCELLENCE = "OperationalExcellence"


@dataclass
class PolicyConfig:
    """Configuration for decision thresholds and grouping."""

    # Impact thresholds: what level triggers a ticket?
    cost_threshold: ImpactLevel = ImpactLevel.MEDIUM
    security_threshold: ImpactLevel = ImpactLevel.HIGH
    reliability_threshold: ImpactLevel = ImpactLevel.MEDIUM
    performance_threshold: ImpactLevel = ImpactLevel.LOW
    operational_excellence_threshold: ImpactLevel = ImpactLevel.MEDIUM

    # Grouping
    group_by_recommendation_type: bool = True
    group_by_resource_group: bool = False
    group_by_subscription: bool = False

    # Max tickets per run (safety limit)
    max_new_tickets_per_run: int = 10

    # Exempted recommendation types (full exclusion - never create tickets)
    exempted_recommendation_types: set[str] = None

    # Postponed: skip findings marked as postponed in Advisor
    skip_postponed: bool = True

    # Cost savings thresholds (for cost category)
    min_monthly_savings_cost: float = 100.0  # Only create tickets for savings > $100/month

    def __post_init__(self):
        """Initialize default values."""
        if self.exempted_recommendation_types is None:
            self.exempted_recommendation_types = set()


class PolicyEngine:
    """Decision policy engine for Azure Advisor recommendations."""

    def __init__(self, config: PolicyConfig = None):
        """
        Initialize the policy engine.

        Args:
            config: PolicyConfig instance. If None, uses defaults.
        """
        self.config = config or PolicyConfig()

    def should_include_finding(self, finding: dict) -> tuple[bool, Optional[str]]:
        """
        Determine if a finding should be included in processing.

        This is the first filter - exempted findings never reach the agent.

        Args:
            finding: The Azure Advisor finding dict with keys:
                - recommendation_type: str
                - suppressed: bool (if suppressed in Advisor)
                - postponed: bool (if postponed in Advisor)
                - exempted: bool (if exempted by policy)

        Returns:
            Tuple of (should_include: bool, reason: Optional[str])
            If False, reason explains why it was excluded.
        """
        recommendation_type = finding.get("recommendation_type", "")

        # Check exemptions (highest priority - never process exempted)
        if finding.get("exempted") or recommendation_type in self.config.exempted_recommendation_types:
            return False, f"Exempted recommendation type: {recommendation_type}"

        # Check suppressed in Advisor
        if finding.get("suppressed"):
            return False, "Suppressed in Azure Advisor"

        # Check postponed
        if self.config.skip_postponed and finding.get("postponed"):
            return False, "Postponed in Azure Advisor"

        return True, None

    def get_impact_threshold(self, category: str) -> ImpactLevel:
        """
        Get the impact threshold for a category.

        Args:
            category: The recommendation category (from Category enum).

        Returns:
            The ImpactLevel threshold for creating tickets in this category.
        """
        category_map = {
            Category.COST.value: self.config.cost_threshold,
            Category.SECURITY.value: self.config.security_threshold,
            Category.RELIABILITY.value: self.config.reliability_threshold,
            Category.PERFORMANCE.value: self.config.performance_threshold,
            Category.OPERATIONAL_EXCELLENCE.value: self.config.operational_excellence_threshold,
        }

        return category_map.get(category, ImpactLevel.MEDIUM)

    def should_create_ticket(
        self,
        finding: dict,
        existing_jira_key: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Decide if a new ticket should be created for this finding.

        Args:
            finding: The Azure Advisor finding with keys:
                - category: str
                - impact: str (High/Medium/Low)
                - estimated_savings: float (for cost findings)
            existing_jira_key: If there's an existing ticket mapped to this finding.

        Returns:
            Tuple of (should_create: bool, reason: Optional[str])
        """
        # Never create if already has a ticket
        if existing_jira_key:
            return False, f"Already has ticket: {existing_jira_key}"

        category = finding.get("category", "")
        impact_str = finding.get("impact", "Low")

        # Parse impact level
        try:
            impact = ImpactLevel(impact_str)
        except ValueError:
            return False, f"Unknown impact level: {impact_str}"

        # Get threshold for this category
        threshold = self.get_impact_threshold(category)

        # Check impact threshold
        impact_order = {ImpactLevel.HIGH: 3, ImpactLevel.MEDIUM: 2, ImpactLevel.LOW: 1}
        if impact_order.get(impact, 0) < impact_order.get(threshold, 0):
            return False, f"Impact {impact.value} below threshold {threshold.value}"

        # Category-specific checks
        if category == Category.COST.value:
            savings = finding.get("estimated_savings", 0)
            if savings < self.config.min_monthly_savings_cost:
                return False, f"Savings ${savings}/month below threshold ${self.config.min_monthly_savings_cost}"

        return True, None

    def decide_action(
        self,
        finding: dict,
        existing_jira_key: Optional[str] = None,
        finding_changed: bool = False,
    ) -> tuple[Decision, str]:
        """
        Determine the action to take for a finding.

        Args:
            finding: The Azure Advisor finding.
            existing_jira_key: The existing Jira ticket if one exists.
            finding_changed: Whether the finding has changed since last run.

        Returns:
            Tuple of (decision: Decision enum, rationale: str)
        """
        # Step 1: Check if should create new ticket
        should_create, reason = self.should_create_ticket(finding, existing_jira_key)

        if should_create and not existing_jira_key:
            return Decision.CREATE, "Meets impact threshold and no existing ticket"

        # Step 2: Check if should update existing ticket
        if existing_jira_key:
            if finding_changed:
                return Decision.UPDATE, "Finding details or severity changed"
            else:
                return Decision.SKIP, "No changes to finding"

        # Step 3: Check if should close (finding no longer exists or is resolved)
        if finding.get("resolved"):
            if existing_jira_key:
                return Decision.CLOSE, "Finding has been resolved"

        # Step 4: Default to skip
        return Decision.SKIP, reason or "Does not meet criteria for ticket creation"

    def format_ticket_summary(self, finding: dict) -> str:
        """
        Format a ticket summary from a finding.

        Args:
            finding: The Azure Advisor finding.

        Returns:
            Formatted summary string suitable for Jira.
        """
        category = finding.get("category", "")
        resource = finding.get("resource", "Unknown")
        impact = finding.get("impact", "")

        return f"[{category}] {finding.get('recommendation_title', 'Advisor Recommendation')} - {resource}"

    def format_ticket_description(self, finding: dict) -> str:
        """
        Format a ticket description from a finding.

        Args:
            finding: The Azure Advisor finding.

        Returns:
            Formatted description suitable for Jira.
        """
        lines = [
            f"Azure Advisor Finding",
            f"",
            f"Category: {finding.get('category', 'Unknown')}",
            f"Impact: {finding.get('impact', 'Unknown')}",
            f"Resource: {finding.get('resource', 'Unknown')}",
            f"Subscription: {finding.get('subscription_id', 'Unknown')}",
            f"",
            f"Recommendation:",
            f"{finding.get('recommendation_text', 'N/A')}",
            f"",
        ]

        if finding.get("estimated_savings"):
            lines.append(f"Estimated Savings: ${finding.get('estimated_savings')}/month")
            lines.append(f"")

        if finding.get("implementation_effort"):
            lines.append(f"Implementation Effort: {finding.get('implementation_effort')}")
            lines.append(f"")

        lines.extend([
            f"Advisor ID: {finding.get('advisor_id', 'N/A')}",
            f"",
            f"Created by: Azure Advisor Task Manager",
        ])

        return "\n".join(lines)

    def get_ticket_priority(self, finding: dict) -> str:
        """
        Determine Jira ticket priority from finding impact.

        Args:
            finding: The Azure Advisor finding.

        Returns:
            Jira priority string (Highest, High, Medium, Low, Lowest).
        """
        impact = finding.get("impact", "Low")

        priority_map = {
            "High": "High",
            "Medium": "Medium",
            "Low": "Low",
        }

        return priority_map.get(impact, "Medium")

    def get_ticket_labels(self, finding: dict) -> list[str]:
        """
        Generate Jira labels for a ticket from the finding.

        Args:
            finding: The Azure Advisor finding.

        Returns:
            List of label strings.
        """
        labels = [
            f"azure-advisor",
            f"category-{finding.get('category', 'unknown').lower()}",
            f"impact-{finding.get('impact', 'unknown').lower()}",
        ]

        if finding.get("estimated_savings"):
            labels.append("cost-optimization")

        return labels
