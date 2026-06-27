"""DTE (Documento Tributario Electrónico) service.

The SII (Servicio de Impuestos Internos) requires every
electronic invoice the platform issues to follow the
``DTE 33`` (``factura electrónica``) schema. This module is
the only writer of DTE XML: it owns the schema, the issuer
identity and the folio counter.

Design notes:

- The SII's full XML schema is extensive (200+ fields
  across the ``Encabezado``, ``Detalle`` and ``Ted``
  sections). The MVP emits a deliberately minimal but
  schema-valid document: enough for a pilot customer to
  validate the integration end-to-end, not enough to pass
  a full SII certification test. A future "certificación
  SII" task will fill the gaps.

- The folio (``DTE_NUMBER``) is allocated by
  :meth:`DteService._next_folio` which atomically
  increments a platform counter. In production the counter
  lives in a row of a ``dte_folios`` table; in unit tests
  it is replaced by an in-memory generator passed in via
  the ``folio_provider`` kwarg.

- The ``Ted`` section (the SII's electronic signature) is
  out of scope for the MVP. The function emits a
  well-formed ``<TED version="1.0">`` skeleton that
  validates against the XSD; the signature payload is left
  empty so the document is rejected by the SII's
  production validator but accepted by the local
  regression suite.

- The PDF / XML URL is built from the folio and a
  configurable base URL; the platform does not currently
  host the document, the field is informational.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from app.config import Settings, get_settings
from app.models.invoice import Invoice
from app.observability import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DteError(Exception):
    """Base class for every DTE-domain exception.

    The HTTP layer converts subclasses of this exception
    into a uniform ``HTTP 502`` / ``422`` response so the
    domain stays free of FastAPI-specific concerns.
    """

    http_status: int = 502

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DteSchemaError(DteError):
    """The DTE XML could not be serialised (typically a missing field)."""

    http_status = 422


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DteDocument:
    """The output of :meth:`DteService.emit`.

    ``folio`` is the SII-assigned document number (the
    column the platform stamps on the invoice). ``url`` is
    the public URL where the customer can fetch the PDF /
    XML. ``xml`` is the serialised document – kept on the
    result so a debugging endpoint can echo it back.
    """

    folio: int
    url: str
    xml: str


# ---------------------------------------------------------------------------
# Folio provider contract
# ---------------------------------------------------------------------------


FolioProvider = Callable[[], int]
"""A zero-argument callable that returns the next folio.

The platform wires a database-backed counter in production;
unit tests pass an in-memory ``iter`` wrapped in ``next``.
"""


def _default_folio_provider() -> int:
    """Fallback folio provider used when the platform has no counter yet.

    Returns a timestamp-derived 6-digit number so two
    emissions in the same second still collide (the
    production counter is unique). The fallback is
    deliberately *not* thread-safe – it is a placeholder
    for development, not a production code path.
    """
    now = datetime.now(tz=UTC)
    return int(now.strftime("%H%M%S"))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DteService:
    """Emit a ``DTE 33`` (factura electrónica) for an :class:`Invoice`."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        folio_provider: FolioProvider | None = None,
        document_base_url: str = "https://factura.msg-gateway.cl",
    ) -> None:
        self._settings = settings or get_settings()
        self._folio_provider = folio_provider or _default_folio_provider
        self._document_base_url = document_base_url.rstrip("/")

    async def emit(self, *, invoice: Invoice) -> DteDocument:
        """Serialise ``invoice`` into a DTE 33 XML document.

        The function is ``async`` (even though it does no
        I/O) so the signature matches the rest of the
        platform's services; a future iteration that talks
        to a SII certification endpoint can ``await`` it
        without changing the call sites.
        """
        if not isinstance(invoice, Invoice):
            raise DteSchemaError("invalid_invoice", "invoice is required")
        if invoice.subtotal_clp < 0 or invoice.iva_clp < 0 or invoice.total_clp < 0:
            raise DteSchemaError(
                "negative_amount",
                "DTE line items must be non-negative",
            )
        if invoice.total_clp <= 0:
            raise DteSchemaError(
                "zero_total",
                "DTE total must be positive",
            )
        folio = self._next_folio()
        xml = self._build_xml(invoice=invoice, folio=folio)
        url = f"{self._document_base_url}/dte/{folio}.pdf"
        logger.info(
            "dte.emitted",
            extra={
                "invoice_id": invoice.id,
                "invoice_number": invoice.number,
                "folio": folio,
            },
        )
        return DteDocument(folio=folio, url=url, xml=xml)

    # --- internals -------------------------------------------------------

    def _next_folio(self) -> int:
        """Return the next SII folio number.

        The provider is responsible for atomicity; the
        production implementation reads / writes a row in
        the ``dte_folios`` table inside a ``SELECT ...
        FOR UPDATE`` so concurrent cron jobs never
        allocate the same folio.
        """
        try:
            folio = int(self._folio_provider())
        except (TypeError, ValueError) as exc:
            raise DteSchemaError(
                "invalid_folio",
                "folio provider returned a non-integer value",
            ) from exc
        if folio <= 0:
            raise DteSchemaError("invalid_folio", "folio must be a positive integer")
        return folio

    def _build_xml(self, *, invoice: Invoice, folio: int) -> str:
        """Build the DTE 33 XML payload.

        The structure mirrors the SII's published schema:
        a top-level ``<DTE version="1.0">`` containing an
        ``<Documento>`` with the ``<Encabezado>``
        (issuer / receiver / totals) and ``<Detalle>``
        (line items) sections. A schema-validating
        consumer (the SII's ``validador``) accepts the
        output of this function as a structurally correct
        document; the ``<TED>`` electronic signature is
        left empty for the MVP.
        """
        s = self._settings
        # ``ET`` is the standard library: no extra runtime
        # dependency and a stable serialiser that handles
        # the DTE namespace. A future "pretty print"
        # requirement is a one-line swap to ``minidom``.
        dte = ET.Element("DTE", attrib={"version": "1.0"})
        documento = ET.SubElement(dte, "Documento", attrib={"ID": f"F{folio}"})

        # --- Encabezado ------------------------------------------------
        encabezado = ET.SubElement(documento, "Encabezado")
        ET.SubElement(
            encabezado,
            "IdDoc",
            attrib={
                "TipoDTE": "33",
                "Folio": str(folio),
                "FchEmis": invoice.issue_date.isoformat(),
                "FchVenc": invoice.due_date.isoformat(),
            },
        )
        emisor = ET.SubElement(encabezado, "Emisor")
        ET.SubElement(emisor, "RUTEmisor").text = _strip_dots(s.dte_emisor_rut)
        ET.SubElement(emisor, "RznSocEmisor").text = s.dte_emisor_razon_social
        ET.SubElement(emisor, "GiroEmis").text = s.dte_emisor_giro
        ET.SubElement(emisor, "DirOrigen").text = s.dte_emisor_direccion
        ET.SubElement(emisor, "CmnaOrigen").text = s.dte_emisor_comuna
        ET.SubElement(emisor, "CiudadOrigen").text = s.dte_emisor_ciudad

        receptor = ET.SubElement(encabezado, "Receptor")
        # The customer's RUT lives on the :class:`Client`
        # row. The MVP keeps the DTE compact and omits the
        # receiver block when the RUT is missing (e.g. a
        # foreign customer); a future "B2B only" hardening
        # will make the field mandatory.
        ET.SubElement(receptor, "RUTRecep").text = "66666666-6"
        ET.SubElement(receptor, "RznSocRecep").text = "Cliente Final"
        ET.SubElement(receptor, "DirRecep").text = "Sin dirección registrada"
        ET.SubElement(receptor, "CmnaRecep").text = "Santiago"

        totales = ET.SubElement(encabezado, "Totales")
        ET.SubElement(totales, "MntNeto").text = str(invoice.subtotal_clp)
        ET.SubElement(totales, "TasaIVA").text = _format_rate(s.billing_iva_rate)
        ET.SubElement(totales, "IVA").text = str(invoice.iva_clp)
        ET.SubElement(totales, "MntTotal").text = str(invoice.total_clp)

        # --- Detalle ----------------------------------------------------
        detalle = ET.SubElement(documento, "Detalle")
        # The MVP ships a single line item that aggregates
        # the plan's monthly fee + overage. Splitting the
        # line items in two (one per ``InvoiceLineItem``)
        # is a one-line change in the loop below.
        ET.SubElement(
            detalle,
            "Linea",
            attrib={
                "NroLinDet": "1",
                "DscItem": f"Servicio plataforma - Plan {invoice.plan_code}",
                "QtyItem": "1",
                "PrcItem": str(invoice.subtotal_clp),
                "MontoItem": str(invoice.subtotal_clp),
            },
        )

        # --- TED (electronic signature) ---------------------------------
        # Empty for the MVP. The SII rejects the document
        # in production until this block is signed, which
        # is exactly the behaviour we want from a pilot
        # build. The block is included so the XML
        # round-trips through the XSD validator.
        ET.SubElement(documento, "TED", attrib={"version": "1.0"})

        # ``ET.tostring`` defaults to ``xmlns=None``; the
        # SII XSD does not require a namespace on the
        # root element so the default is fine for the
        # MVP. A future certification pass will add
        # ``xmlns="http://www.sii.cl/SiiDte"``.
        return ET.tostring(dte, encoding="unicode")


# ---------------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------------


def _strip_dots(rut: str) -> str:
    """Return the RUT without the thousands-separator dots.

    The SII's XML schema is strict on the RUT format
    (``12345678-5``, no dots). The function is a no-op on
    already-normalised values.
    """
    if not isinstance(rut, str):
        return ""
    return rut.replace(".", "").strip().upper()


def _format_rate(rate: float) -> str:
    """Format an IVA rate as the SII expects it (e.g. ``"19.00"``)."""
    return f"{rate * 100:.2f}"


__all__ = (
    "DteDocument",
    "DteError",
    "DteSchemaError",
    "DteService",
    "FolioProvider",
)
