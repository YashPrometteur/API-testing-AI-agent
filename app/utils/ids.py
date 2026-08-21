import uuid


def generate_run_id() -> str:
    """Generate unique run ID."""
    return f"run_{uuid.uuid4().hex[:12]}"


def generate_test_case_id(category: str = "") -> str:
    """Generate globally unique test case ID prefixed by category."""
    cat_prefix = f"tc_{category.lower()}" if category else "tc"
    return f"{cat_prefix}_{uuid.uuid4().hex[:8]}"


def generate_finding_id() -> str:
    """Generate unique finding ID."""
    return f"fnd_{uuid.uuid4().hex[:12]}"
