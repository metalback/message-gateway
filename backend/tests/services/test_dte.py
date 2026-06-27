"""Unit tests for the DTE (Documento Tributario Electrónico) service.

The tests assert the XML contract the SII expects: the
``Encabezado`` carries the issuer identity, the ``Detalle``
has the line items, and the document is well-formed (the
``xml.etree.ElementTree`` parser round-trips it).

The DTE service never talks to the network – the tests
inject an in-memory folio provider so two emissions
return distinct folios deterministically.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import date
from xml.etree import ElementTree as ET

import pytest

from app.config import Settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.plan import Plan
from app.services.dte import DteDocument, DteSchemaError, DteService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """Settings with predictable billing defaults for unit tests."""
    return Settings(
        secret_key="test-secret",
        billing_iva_rate=0.19,
    )


@pytest.fixture
def plan() -> Plan:
    """A minimal :class:`Plan` row used to populate ``Invoice.plan_code``."""
    return Plan(
        code="starter",
        name="Starter",
        price_clp=19990,
        msg_limit=1000,
        extra_msg_price=25,
    )


def _make_invoice(
    *,
    plan: Plan,
    subtotal: int = 19990,
    iva: int = 3798,
    total: int = 23788,
) -> Invoice:
    """Build a :class:`Invoice` row that mirrors what the billing service writes."""
    return Invoice(
        number="F-2026-ABCDEF",
        client_id="client-1",
        plan_id=plan.id if hasattr(plan, "id") else "plan-1",
        plan_code=plan.code,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        total_msgs=1000,
        included_msgs=1000,
        overage_msgs=0,
        subtotal_clp=subtotal,
        iva_clp=iva,
        total_clp=total,
        status=InvoiceStatus.DRAFT,
        issue_date=date(2026, 6, 30),
        due_date=date(2026, 7, 30),
    )


def _counter() -> Callable[[], int]:
    """Return a callable that yields successive folio numbers."""
    counter = itertools.count(1)

    def provider() -> int:
        return next(counter)

    return provider


# ---------------------------------------------------------------------------
# DteService.emit
# ---------------------------------------------------------------------------


async def test_emit_returns_dte_document(
    fast_settings: Settings, plan: Plan
) -> None:
    """A successful emission returns a :class:`DteDocument` with all fields."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)

    assert isinstance(doc, DteDocument)
    assert doc.folio == 1
    assert doc.url.endswith("/1.pdf")
    assert doc.xml.startswith("<DTE")


async def test_emit_xml_round_trips_through_element_tree(
    fast_settings: Settings, plan: Plan
) -> None:
    """The serialised XML is well-formed (parses back without error)."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)
    # Raises :class:`xml.etree.ElementTree.ParseError` on
    # malformed XML; the test passes if no exception is
    # raised.
    root = ET.fromstring(doc.xml)
    assert root.tag == "DTE"
    assert root.attrib.get("version") == "1.0"


async def test_emit_includes_issuer_identity(
    fast_settings: Settings, plan: Plan
) -> None:
    """The ``Encabezado/Emisor`` block mirrors the platform's :class:`Settings`."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)
    root = ET.fromstring(doc.xml)
    emisor = root.find(".//Emisor")
    assert emisor is not None
    # The RUT is normalised (dots stripped) per the SII's
    # XML schema requirement.
    assert emisor.findtext("RUTEmisor") == "76123456-7"
    assert emisor.findtext("RznSocEmisor") == "Message Gateway SpA"
    assert emisor.findtext("GiroEmis") == "Servicios de Telecomunicaciones"


async def test_emit_includes_totals_block(
    fast_settings: Settings, plan: Plan
) -> None:
    """The ``Totales`` block carries subtotal, IVA rate, IVA, and total."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan, subtotal=19990, iva=3798, total=23788)

    doc = await service.emit(invoice=invoice)
    root = ET.fromstring(doc.xml)
    totales = root.find(".//Totales")
    assert totales is not None
    assert totales.findtext("MntNeto") == "19990"
    assert totales.findtext("TasaIVA") == "19.00"
    assert totales.findtext("IVA") == "3798"
    assert totales.findtext("MntTotal") == "23788"


async def test_emit_includes_detalle_line(
    fast_settings: Settings, plan: Plan
) -> None:
    """The ``Detalle`` section carries at least one ``Linea`` element."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)
    root = ET.fromstring(doc.xml)
    detalle = root.find(".//Detalle")
    assert detalle is not None
    lineas = detalle.findall("Linea")
    assert len(lineas) == 1
    linea = lineas[0]
    assert linea.attrib["NroLinDet"] == "1"
    assert "starter" in linea.attrib["DscItem"]
    assert linea.attrib["MontoItem"] == str(invoice.subtotal_clp)


async def test_emit_uses_dte_33_type(fast_settings: Settings, plan: Plan) -> None:
    """The SII's DTE 33 (factura electrónica) is the only type the MVP ships."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)
    root = ET.fromstring(doc.xml)
    id_doc = root.find(".//IdDoc")
    assert id_doc is not None
    assert id_doc.attrib["TipoDTE"] == "33"
    assert id_doc.attrib["Folio"] == "1"


async def test_emit_allocates_unique_folios(
    fast_settings: Settings, plan: Plan
) -> None:
    """Two consecutive emissions land on different folio numbers."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice_a = _make_invoice(plan=plan)
    invoice_b = _make_invoice(plan=plan)

    a = await service.emit(invoice=invoice_a)
    b = await service.emit(invoice=invoice_b)
    assert a.folio == 1
    assert b.folio == 2
    assert a.url != b.url


async def test_emit_url_uses_configured_base(
    fast_settings: Settings, plan: Plan
) -> None:
    """The document URL honours the ``document_base_url`` constructor kwarg."""
    service = DteService(
        settings=fast_settings,
        folio_provider=_counter(),
        document_base_url="https://custom.example.com",
    )
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)
    assert doc.url.startswith("https://custom.example.com/dte/")
    assert doc.url.endswith("/1.pdf")


async def test_emit_rejects_invalid_invoice(
    fast_settings: Settings,
) -> None:
    """Passing a non-Invoice raises :class:`DteSchemaError`."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    with pytest.raises(DteSchemaError) as exc:
        await service.emit(invoice="not-an-invoice")  # type: ignore[arg-type]
    assert exc.value.code == "invalid_invoice"


async def test_emit_rejects_zero_total(
    fast_settings: Settings, plan: Plan
) -> None:
    """An invoice with ``total_clp <= 0`` is rejected (SII requires a positive total)."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan, subtotal=0, iva=0, total=0)
    with pytest.raises(DteSchemaError) as exc:
        await service.emit(invoice=invoice)
    assert exc.value.code == "zero_total"


async def test_emit_rejects_negative_amounts(
    fast_settings: Settings, plan: Plan
) -> None:
    """A negative line item is a schema error."""
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan, subtotal=-1, iva=0, total=-1)
    with pytest.raises(DteSchemaError) as exc:
        await service.emit(invoice=invoice)
    assert exc.value.code == "negative_amount"


async def test_emit_rejects_invalid_folio_provider(
    fast_settings: Settings, plan: Plan
) -> None:
    """A folio provider that returns a non-integer raises :class:`DteSchemaError`."""
    def bad_provider() -> int:
        return "not-an-int"  # type: ignore[return-value]

    service = DteService(settings=fast_settings, folio_provider=bad_provider)
    invoice = _make_invoice(plan=plan)
    with pytest.raises(DteSchemaError) as exc:
        await service.emit(invoice=invoice)
    assert exc.value.code == "invalid_folio"


async def test_emit_rejects_zero_folio(
    fast_settings: Settings, plan: Plan
) -> None:
    """A folio of zero (or negative) is rejected – the SII starts at 1."""
    service = DteService(settings=fast_settings, folio_provider=lambda: 0)
    invoice = _make_invoice(plan=plan)
    with pytest.raises(DteSchemaError) as exc:
        await service.emit(invoice=invoice)
    assert exc.value.code == "invalid_folio"


async def test_emit_iva_rate_is_formatted_with_two_decimals(
    fast_settings: Settings, plan: Plan
) -> None:
    """The SII expects the IVA rate as a fixed-point string (``"19.00"``)."""
    fast_settings.billing_iva_rate = 0.19
    service = DteService(settings=fast_settings, folio_provider=_counter())
    invoice = _make_invoice(plan=plan)

    doc = await service.emit(invoice=invoice)
    root = ET.fromstring(doc.xml)
    tasa_iva = root.findtext(".//Totales/TasaIVA")
    assert tasa_iva == "19.00"
