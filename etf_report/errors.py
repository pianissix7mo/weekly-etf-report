class ETFReportError(RuntimeError):
    """Base class for pipeline failures that should be surfaced to CI."""

    code = "ETF_REPORT_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class SourceUnavailableError(ETFReportError):
    code = "SOURCE_UNAVAILABLE"


class SourceSchemaChangedError(ETFReportError):
    code = "SOURCE_SCHEMA_CHANGED"


class HoldingsWeightInvalidError(ETFReportError):
    code = "HOLDINGS_WEIGHT_INVALID"


class ReportIncompleteError(ETFReportError):
    code = "REPORT_INCOMPLETE"
