"""
Approval workflow for Jira ticket changes.

Handles the human-in-the-loop approval process:
- Displaying proposed changes
- Collecting approvals/rejections/edits
- Tracking rejections to avoid re-proposing
"""

import json
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from enum import Enum


class ApprovalDecision(Enum):
    """User's decision on a proposed change."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    PENDING = "pending"


@dataclass
class ProposedChange:
    """A proposed change to a Jira ticket."""

    advisor_id: str
    action: str  # create, update, close
    jira_key: Optional[str]  # None for create actions
    summary: str
    description: str
    priority: str
    labels: List[str]
    rationale: str
    estimated_savings: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return asdict(self)


@dataclass
class ApprovalResponse:
    """User's response to a proposed change."""

    advisor_id: str
    decision: ApprovalDecision
    timestamp: str
    edited_fields: Optional[Dict] = None
    comment: Optional[str] = None
    user: Optional[str] = None


class ApprovalUI:
    """Interface for human approval of proposed changes."""

    def __init__(self):
        """Initialize approval UI."""
        self.responses: List[ApprovalResponse] = []

    def display_proposal(self, change: ProposedChange) -> None:
        """
        Display a proposed change to the user.

        Args:
            change: The ProposedChange to display.
        """
        print("\n" + "=" * 70)
        print(f"Proposed: {change.action.upper()}")
        print("=" * 70)

        if change.action == "create":
            print(f"\nCreate new Jira ticket:")
            print(f"  Summary: {change.summary}")
            print(f"  Priority: {change.priority}")
            print(f"  Labels: {', '.join(change.labels)}")
            if change.estimated_savings:
                print(f"  Estimated Savings: {change.estimated_savings}")

        elif change.action == "update":
            print(f"\nUpdate Jira ticket: {change.jira_key}")
            print(f"  Summary: {change.summary}")
            print(f"  Priority: {change.priority}")

        elif change.action == "close":
            print(f"\nClose Jira ticket: {change.jira_key}")

        print(f"\nRationale:")
        print(f"  {change.rationale}")

        print(f"\nDescription:")
        for line in change.description.split("\n"):
            print(f"  {line}")

        print(f"\nAdvisor ID: {change.advisor_id}")

    def display_summary_table(self, changes: List[ProposedChange]) -> None:
        """
        Display a table summary of all proposed changes.

        Args:
            changes: List of ProposedChange objects.
        """
        print("\n" + "=" * 100)
        print("PROPOSED CHANGES SUMMARY")
        print("=" * 100)

        # Header
        print(
            f"{'#':<3} {'Action':<8} {'Jira':<12} {'Summary':<40} {'Priority':<8} {'Savings':<12}"
        )
        print("-" * 100)

        # Rows
        for i, change in enumerate(changes, 1):
            jira = change.jira_key or "NEW"
            summary = change.summary[:37] + "..." if len(change.summary) > 40 else change.summary
            savings = change.estimated_savings or "-"

            print(
                f"{i:<3} {change.action:<8} {jira:<12} {summary:<40} {change.priority:<8} {savings:<12}"
            )

        print("-" * 100)
        print(f"Total: {len(changes)} proposed changes")

    def get_approval_from_user(
        self,
        change: ProposedChange,
        index: int = 1,
        total: int = 1,
    ) -> ApprovalResponse:
        """
        Get user approval for a proposed change (interactive).

        Args:
            change: The ProposedChange to approve.
            index: Current proposal number (for display).
            total: Total number of proposals.

        Returns:
            ApprovalResponse with user's decision.
        """
        from datetime import datetime

        self.display_proposal(change)
        print(f"\n[{index}/{total}]")

        while True:
            user_input = input(
                "\nApprove? (a=approve, r=reject, e=edit, s=skip): "
            ).strip().lower()

            if user_input == "a":
                return ApprovalResponse(
                    advisor_id=change.advisor_id,
                    decision=ApprovalDecision.APPROVED,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                    user="unknown",
                )

            elif user_input == "r":
                comment = input("Rejection reason: ").strip()
                return ApprovalResponse(
                    advisor_id=change.advisor_id,
                    decision=ApprovalDecision.REJECTED,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                    comment=comment,
                    user="unknown",
                )

            elif user_input == "e":
                field = input("Which field to edit? (summary/description/priority): ").strip().lower()
                new_value = input(f"New {field}: ").strip()

                if field in ["summary", "description", "priority"]:
                    setattr(change, field, new_value)

                    return ApprovalResponse(
                        advisor_id=change.advisor_id,
                        decision=ApprovalDecision.EDITED,
                        timestamp=datetime.utcnow().isoformat() + "Z",
                        edited_fields={field: new_value},
                        user="unknown",
                    )

                print("Invalid field. Try again.")

            elif user_input == "s":
                return ApprovalResponse(
                    advisor_id=change.advisor_id,
                    decision=ApprovalDecision.PENDING,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                    comment="Skipped by user",
                    user="unknown",
                )

            else:
                print("Invalid input. Use a, r, e, or s.")

    def get_batch_approvals_from_user(
        self, changes: List[ProposedChange]
    ) -> Dict[str, ApprovalResponse]:
        """
        Get user approval for multiple changes.

        Args:
            changes: List of ProposedChange objects.

        Returns:
            Dict mapping advisor_id to ApprovalResponse.
        """
        self.display_summary_table(changes)

        print("\n" + "=" * 70)
        print("REVIEWING EACH PROPOSAL")
        print("=" * 70)

        responses = {}

        for i, change in enumerate(changes, 1):
            response = self.get_approval_from_user(change, i, len(changes))
            responses[change.advisor_id] = response
            self.responses.append(response)

        return responses

    def display_approval_summary(self, responses: Dict[str, ApprovalResponse]) -> None:
        """
        Display a summary of all approvals.

        Args:
            responses: Dict of approval responses.
        """
        approved = sum(1 for r in responses.values() if r.decision == ApprovalDecision.APPROVED)
        rejected = sum(1 for r in responses.values() if r.decision == ApprovalDecision.REJECTED)
        edited = sum(1 for r in responses.values() if r.decision == ApprovalDecision.EDITED)
        pending = sum(1 for r in responses.values() if r.decision == ApprovalDecision.PENDING)

        print("\n" + "=" * 70)
        print("APPROVAL SUMMARY")
        print("=" * 70)
        print(f"✓ Approved: {approved}")
        print(f"✗ Rejected: {rejected}")
        print(f"✎ Edited:   {edited}")
        print(f"⊘ Pending:  {pending}")
        print(f"\nTotal: {len(responses)}")

    def save_approvals(self, responses: Dict[str, ApprovalResponse], filepath: str) -> None:
        """
        Save approval responses to a JSON file.

        Args:
            responses: Dict of approval responses.
            filepath: Path to save JSON file.
        """
        data = {
            advisor_id: {
                "decision": response.decision.value,
                "timestamp": response.timestamp,
                "edited_fields": response.edited_fields,
                "comment": response.comment,
                "user": response.user,
            }
            for advisor_id, response in responses.items()
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

    def load_approvals(self, filepath: str) -> Dict[str, ApprovalResponse]:
        """
        Load approval responses from a JSON file.

        Args:
            filepath: Path to JSON file with saved approvals.

        Returns:
            Dict of ApprovalResponse objects.
        """
        with open(filepath, "r") as f:
            data = json.load(f)

        responses = {}
        for advisor_id, resp_data in data.items():
            response = ApprovalResponse(
                advisor_id=advisor_id,
                decision=ApprovalDecision(resp_data["decision"]),
                timestamp=resp_data["timestamp"],
                edited_fields=resp_data.get("edited_fields"),
                comment=resp_data.get("comment"),
                user=resp_data.get("user"),
            )
            responses[advisor_id] = response

        return responses
