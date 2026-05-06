"""
customer_verification.py (Deterministic)
─────────────────────────────────────────────────
Deterministic customer validation without confidence scoring.

Uses enriched customer data from WooCommerce endpoint:
  - Fetched from /wp-json/wc/v3/customers/{id}
  - Contains accurate VAT number (from meta_data.billing_eu_vat_number)
  - Contains company name (from billing.company)
  - NO fuzzy matching, NO confidence scores

Validation Tree:
  1. Exact email match in Odoo? → decision: "link_existing"
  2. Has VAT or company_name? → customer_type: "bedrijf" (B2B)
  3. No VAT and no company_name? → customer_type: "persoon" (B2C)
  4. Return: {decision, customer_type, validation_checks}
"""

import logging
from typing import Optional
from dataclasses import dataclass, field
from types import SimpleNamespace

logger = logging.getLogger(__name__)


@dataclass
class ValidationCheck:
    """Result of a single validation check."""
    name: str
    passed: bool
    details: str = ""


@dataclass
class ValidationResult:
    """Result of customer validation."""
    decision: str  # "create_new" | "link_existing"
    customer_type: Optional[str]  # "bedrijf" | "persoon" | None
    validation_checks: list[ValidationCheck] = field(default_factory=list)
    recommended_partner_id: Optional[int] = None
    reasoning: str = ""

    def to_dict(self) -> dict:
        """Convert to dict for logging/serialization."""
        return {
            "decision": self.decision,
            "customer_type": self.customer_type,
            "recommended_partner_id": self.recommended_partner_id,
            "validation_checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "details": check.details,
                }
                for check in self.validation_checks
            ],
            "reasoning": self.reasoning,
        }


class CustomerValidator:
    """
    Deterministic customer validation without confidence scoring.
    
    Usage:
        validator = CustomerValidator()
        validator.set_odoo_customers(odoo_customer_list)
        
        woo_customer = woo.get_customer(9)  # Full customer data from endpoint
        result = validator.validate_customer(woo_customer)
        
        if result.decision == "link_existing":
            # Use result.recommended_partner_id
        else:
            # Create new customer with result.customer_type
    """

    def __init__(self):
        """Initialize the validator."""
        self.odoo_customers: list[dict] = []

    def set_odoo_customers(self, customers: list[dict]) -> None:
        """Set the list of Odoo partners to match against."""
        self.odoo_customers = customers
        logger.info("Validator: %d Odoo customers loaded for matching", len(customers))

    def validate_customer(
        self,
        woo_customer: dict,
        odoo_customers: Optional[list[dict]] = None,
    ) -> ValidationResult:
        """
        Validate a WooCommerce customer deterministically.

        Args:
            woo_customer: Full customer dict from /wp-json/wc/v3/customers/{id} endpoint
                          (contains email, first_name, last_name, billing, meta_data)
            odoo_customers: Optional list to override self.odoo_customers

        Returns:
            ValidationResult with decision, customer_type, and validation_checks
        """
        if odoo_customers is not None:
            self.odoo_customers = odoo_customers

        checks: list[ValidationCheck] = []

        # Extract key fields from WooCommerce customer
        woo_email = (woo_customer.get("email") or "").strip().lower()
        woo_company = (woo_customer.get("company_name") or "").strip()
        woo_vat = (woo_customer.get("vat_number") or "").strip()
        woo_first_name = (woo_customer.get("first_name") or "").strip()
        woo_last_name = (woo_customer.get("last_name") or "").strip()

        # If no email, we can't do anything deterministic
        if not woo_email:
            logger.warning("Validator: Customer has no email – cannot validate")
            return ValidationResult(
                decision="create_new",
                customer_type="persoon",
                reasoning="No email provided – defaulting to create_new with type persoon",
            )


        # STEP 1: Exact Email Match (highest priority – deterministic)
        email_match = self._find_exact_email_match(woo_email)

        checks.append(
            ValidationCheck(
                name="exact_email_match",
                passed=email_match is not None,
                details=f"Email: {woo_email}",
            )
        )

        if email_match:
            logger.info(
                "Validator: Exact email match found → linking to Odoo partner id=%s (%s)",
                email_match["id"],
                email_match.get("name", ""),
            )
            return ValidationResult(
                decision="link_existing",
                customer_type=None,  # No type needed for linking
                recommended_partner_id=email_match["id"],
                validation_checks=checks,
                reasoning=f"Exact email match: {woo_email} → partner id={email_match['id']}",
            )

        # STEP 2: Determine Customer Type (deterministic: B2B or B2C)
        # Check for VAT number (first indicator of B2B)
        has_vat = bool(woo_vat)
        checks.append(
            ValidationCheck(
                name="has_vat_number",
                passed=has_vat,
                details=f"VAT: {woo_vat if woo_vat else '(empty)'}",
            )
        )

        # Check for company name (second indicator of B2B)
        has_company = bool(woo_company)
        checks.append(
            ValidationCheck(
                name="has_company_name",
                passed=has_company,
                details=f"Company: {woo_company if woo_company else '(empty)'}",
            )
        )

        # Deterministic classification:
        # - VAT OR company_name → B2B (bedrijf)
        # - No VAT AND no company_name → B2C (persoon)
        if has_vat or has_company:
            customer_type = "bedrijf"
            type_reasoning = (
                f"B2B (bedrijf) because: "
                f"{'has VAT' if has_vat else ''} "
                f"{'and ' if has_vat and has_company else ''} "
                f"{'has company name' if has_company else ''}".strip()
            )
        else:
            customer_type = "persoon"
            type_reasoning = "B2C (persoon) because: no VAT and no company name"

        checks.append(
            ValidationCheck(
                name="customer_type_determination",
                passed=True,
                details=type_reasoning,
            )
        )

        # STEP 3: Return Decision (always create_new for new customers)

        logger.info(
            "Validator: New customer (no email match) → create_new type=%s "
            "(email=%s company=%s vat=%s)",
            customer_type,
            woo_email,
            woo_company or "(geen)",
            woo_vat or "(geen)",
        )

        return ValidationResult(
            decision="create_new",
            customer_type=customer_type,
            validation_checks=checks,
            reasoning=f"No existing Odoo customer found. Will create new {customer_type}: {woo_email}",
        )

    def _find_exact_email_match(self, email: str) -> Optional[dict]:
        """
        Find an exact email match in Odoo customers (case-insensitive).

        Args:
            email: Normalized email (lowercase, stripped)

        Returns:
            Odoo customer dict if found, None otherwise
        """
        email_lower = email.lower().strip()

        for customer in self.odoo_customers:
            odoo_email = (customer.get("email") or "").lower().strip()
            if odoo_email == email_lower:
                logger.debug(
                    "Validator: Exact email match found – Odoo id=%s name=%s",
                    customer.get("id"),
                    customer.get("name"),
                )
                return customer

        return None


class CustomerVerifier:
    """
    Backward compatibility wrapper for the old verifier interface.
    
    This wrapper converts the old method signature to use the new validator.
    Allows existing code to continue working without modification.
    """

    def __init__(self, odoo_customers: list[dict] | None = None):
        """Initialize the verifier."""
        self.validator = CustomerValidator()
        if odoo_customers:
            self.validator.set_odoo_customers(odoo_customers)

    def set_odoo_customers(self, customers: list[dict]) -> None:
        """Set Odoo customers for the validator."""
        self.validator.set_odoo_customers(customers)

    def verify_woo_order_customer(
        self,
        woo_customer_id: int,
        woo_email: str,
        woo_name: str,
        woo_phone: str | None,
        woo_company: str | None,
        woo_vat: str | None,
        billing_address=None,
        shipping_address=None,
        woo_customer: dict | None = None,
    ):
        """
        Backward compatibility method for old verifier interface.
        
        This converts the old individual-field API to the new customer-dict API.
        
        Args:
            woo_customer_id: WooCommerce customer ID
            woo_email: Customer email
            woo_name: Customer name
            woo_phone: Customer phone
            woo_company: Customer company name
            woo_vat: Customer VAT number
            billing_address: Billing address object (deprecated)
            shipping_address: Shipping address object (deprecated)
            woo_customer: (NEW) Full customer dict from WC endpoint (takes priority over individual fields)
        
        ⚠️ DEPRECATED: Use validate_customer(woo_customer_dict) instead.
           This method will be removed once process_order() is updated to use
           the new validate_customer() signature with enriched WC customer data.
        """
        # If enriched customer data provided, use it directly
        if woo_customer:
            logger.debug(
                "verify_woo_order_customer() called with enriched woo_customer data – using new validator"
            )
            return self._convert_validation_result(
                self.validator.validate_customer(woo_customer)
            )

        logger.warning(
            "verify_woo_order_customer() called with individual fields – using DEPRECATED signature. "
            "Please update to pass enriched woo_customer dict."
        )

        # Convert old individual fields to new customer dict format
        woo_cust = {
            "id": woo_customer_id,
            "email": woo_email,
            "first_name": woo_name.split()[0] if woo_name else "",
            "last_name": " ".join(woo_name.split()[1:]) if woo_name and len(woo_name.split()) > 1 else "",
            "company_name": woo_company or "",
            "vat_number": woo_vat or "",
            "phone": woo_phone or "",
        }

        # Use new validator
        validation_result = self.validator.validate_customer(woo_cust)

        # Convert ValidationResult to old VerificationResult format for compatibility
        return self._convert_validation_result(validation_result)

    def _convert_validation_result(self, validation_result: ValidationResult) -> SimpleNamespace:
        """Convert new ValidationResult to old VerificationResult format for backward compatibility."""
        # Map new decision to old fields
        if validation_result.decision == "link_existing":
            verification_status = "auto_matched"
            recommended_action = "link_existing"
        else:
            verification_status = "new_customer"
            recommended_action = "create_new"

        # Create a mock classification object for the old interface
        class MockClassification:
            def __init__(self, customer_type):
                self.customer_type = customer_type
                self.confidence = 1.0  # Deterministic = 100% confidence
                self.confidence_percentage = 100.0

        # Return old format as object with attributes to preserve legacy access pattern.
        return SimpleNamespace(
            verification_status=verification_status,
            recommended_action=recommended_action,
            recommended_partner_id=validation_result.recommended_partner_id,
            issue_description=validation_result.reasoning,
            classification=MockClassification(validation_result.customer_type),
            exact_match_found=validation_result.decision == "link_existing",
        )

