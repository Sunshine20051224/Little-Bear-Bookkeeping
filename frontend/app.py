"""Streamlit frontend for personal expense tracking and budget alerts."""

import os
from dotenv import load_dotenv
import httpx
import pandas as pd
import streamlit as st


load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001").rstrip("/")
CATEGORIES = ["Food", "Transport", "Daily", "Entertainment", "Other"]
REQUEST_TIMEOUT = 10.0


def fetch_summary() -> tuple[list[dict] | None, str | None]:
    """Fetch category budget summary from FastAPI backend."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(f"{BACKEND_URL}/summary")
            resp.raise_for_status()
            return resp.json(), None
    except httpx.HTTPError as exc:
        return None, f"Failed to load budget summary from API: {exc}"


def fetch_expenses(category: str | None = None) -> tuple[list[dict] | None, str | None]:
    """Fetch expense records from FastAPI backend, optionally filtered by category."""
    params = {"category": category} if category and category != "All" else {}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(f"{BACKEND_URL}/expenses", params=params)
            resp.raise_for_status()
            return resp.json(), None
    except httpx.HTTPError as exc:
        return None, f"Failed to load expenses from API: {exc}"


def create_expense_via_api(category: str, amount: float, note: str) -> tuple[dict | None, str | None]:
    """Submit a new expense record to FastAPI backend for rule evaluation and storage."""
    payload = {"category": category, "amount": amount, "note": note}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.post(f"{BACKEND_URL}/expenses", json=payload)
            if resp.status_code >= 400:
                detail = resp.json().get("detail", resp.text) if resp.content else resp.text
                return None, f"API rejected request ({resp.status_code}): {detail}"
            return resp.json(), None
    except httpx.HTTPError as exc:
        return None, f"Network error while creating expense: {exc}"


def main() -> None:
    """Render Streamlit UI for expense entry, budget visualization, and record listing."""
    st.set_page_config(page_title="Expense & Budget Alert Tracker", layout="wide")
    st.title("Personal Expense & Monthly Budget Alert")
    st.caption(f"Connected API: {BACKEND_URL}")

    # 1. User Action: Add a new expense record via FastAPI
    st.subheader("1. Record New Expense")
    with st.form("expense_form", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 3])
        with col1:
            selected_cat = st.selectbox("Category", options=CATEGORIES)
        with col2:
            amount = st.number_input("Amount", min_value=0.01, step=10.0, value=50.0)
        with col3:
            note = st.text_input("Note (optional)", placeholder="e.g., Lunch or bus ticket")

        submitted = st.form_submit_button("Add Expense")
        if submitted:
            result, err = create_expense_via_api(
                category=selected_cat,
                amount=float(amount),
                note=note,
            )
            if err:
                st.error(err)
            elif result is not None:
                status_label = result["status"]
                msg = (
                    f"Saved #{result['expense']['id']} | Status: {status_label} | "
                    f"Spent: {result['updated_spent']} / {result['monthly_budget']} "
                    f"({result['usage_ratio'] * 100:.1f}%)"
                )
                if status_label == "EXCEEDED":
                    st.warning(f"{msg} | Over budget by: {result['over_amount']}")
                elif status_label == "WARNING":
                    st.info(f"{msg} | Approaching monthly budget limit.")
                else:
                    st.success(msg)

    st.divider()

    # 2. Visualization: Category spending vs monthly budget chart
    st.subheader("2. Monthly Budget Overview")
    summary_data, summary_err = fetch_summary()
    if summary_err:
        st.error(summary_err)
    elif not summary_data:
        st.info("No budget summary data available yet.")
    else:
        summary_df = pd.DataFrame(summary_data)
        chart_df = summary_df.set_index("category")[["total_spent", "monthly_budget"]]
        st.bar_chart(chart_df)
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.divider()

    # 3. User Action: Filter and inspect persisted expense records
    st.subheader("3. Expense Records")
    filter_cat = st.selectbox("Filter by Category", options=["All"] + CATEGORIES)
    expenses_data, expenses_err = fetch_expenses(category=filter_cat)
    if expenses_err:
        st.error(expenses_err)
    elif not expenses_data:
        st.info("No expense records found for the selected filter.")
    else:
        expenses_df = pd.DataFrame(expenses_data)
        st.dataframe(expenses_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()