from pathlib import Path
from re import sub

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from config import BASE_DIR, DATA_DIR


class WebQuizPdfService:
    def __init__(self) -> None:
        self.output_dir = DATA_DIR / "generated_pdfs" / "quiz_results"
        self.font_name = "QuizResultPdfFont"
        self._font_registered = False

    def generate_result_pdf(
        self,
        *,
        user_name: str,
        summary: dict,
        leaderboard_rows: list[dict],
        rank_zone_rows: list[dict],
        show_rank_zone: bool,
    ) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._register_font()

        file_name = (
            f"{self._slugify(user_name)}_"
            f"{self._slugify(summary.get('set_title') or 'quiz')}_"
            f"{self._slugify(summary.get('completed_at') or summary.get('started_at') or 'result')}.pdf"
        )
        file_path = self.output_dir / file_name

        pdf = canvas.Canvas(str(file_path), pagesize=A4)
        width, height = A4
        left = 16 * mm
        right = width - (16 * mm)
        top = height - (16 * mm)
        bottom = 16 * mm
        y = top

        y = self._draw_heading(pdf, left, y, summary)
        y = self._draw_summary_stats(pdf, left, right, y, summary)
        y = self._draw_meta(pdf, left, y, summary)
        y = self._draw_table(
            pdf,
            left,
            right,
            y,
            title="Top 10 Rankers",
            rows=leaderboard_rows,
            bottom=bottom,
        )
        if show_rank_zone and rank_zone_rows:
            y = self._draw_table(
                pdf,
                left,
                right,
                y,
                title="Your Rank Zone",
                rows=rank_zone_rows,
                bottom=bottom,
            )

        pdf.save()
        return file_path

    def _draw_heading(self, pdf: canvas.Canvas, left: float, y: float, summary: dict) -> float:
        pdf.setFillColor(colors.HexColor("#16324f"))
        pdf.setFont(self.font_name, 18)
        pdf.drawString(left, y, "Quiz Result")
        y -= 20
        pdf.setFillColor(colors.HexColor("#4b5563"))
        pdf.setFont(self.font_name, 11)
        pdf.drawString(left, y, f"Set: {summary.get('set_title') or 'Quiz Set'}")
        return y - 18

    def _draw_summary_stats(self, pdf: canvas.Canvas, left: float, right: float, y: float, summary: dict) -> float:
        box_height = 58
        box_gap = 6
        box_count = 5
        box_width = ((right - left) - (box_gap * (box_count - 1))) / box_count
        stats = (
            ("Score", f"{float(summary.get('score') or 0):.2f}"),
            ("Accuracy", f"{float(summary.get('accuracy') or 0):.2f}%"),
            ("Progress", f"{float(summary.get('progress_percent') or 0):.2f}%"),
            ("Correct", str(int(summary.get("correct") or 0))),
            ("Rank", f"#{summary.get('rank')}" if summary.get("rank") else "-"),
        )
        x = left
        for label, value in stats:
            pdf.setFillColor(colors.HexColor("#f8fbff"))
            pdf.roundRect(x, y - box_height, box_width, box_height, 8, fill=1, stroke=0)
            pdf.setStrokeColor(colors.HexColor("#cbd5e1"))
            pdf.roundRect(x, y - box_height, box_width, box_height, 8, fill=0, stroke=1)
            pdf.setFillColor(colors.HexColor("#64748b"))
            pdf.setFont(self.font_name, 8.5)
            pdf.drawString(x + 8, y - 16, label)
            pdf.setFillColor(colors.HexColor("#111827"))
            pdf.setFont(self.font_name, 12)
            pdf.drawString(x + 8, y - 35, value)
            x += box_width + box_gap
        return y - box_height - 16

    def _draw_meta(self, pdf: canvas.Canvas, left: float, y: float, summary: dict) -> float:
        pdf.setFillColor(colors.HexColor("#111827"))
        pdf.setFont(self.font_name, 10.5)
        lines = (
            f"Attempted: {int(summary.get('attempted') or 0)} / {int(summary.get('requested_count') or 0)}",
            f"Skipped: {int(summary.get('skipped') or 0)}",
            f"Started at: {summary.get('started_at_display') or '-'}",
            f"Completed at: {summary.get('completed_at_display') or '-'}",
            f"Ended reason: {str(summary.get('ended_reason') or '').replace('_', ' ').title()}",
        )
        for line in lines:
            pdf.drawString(left, y, line)
            y -= 14
        return y - 10

    def _draw_table(
        self,
        pdf: canvas.Canvas,
        left: float,
        right: float,
        y: float,
        *,
        title: str,
        rows: list[dict],
        bottom: float,
    ) -> float:
        required_height = 34 + (max(len(rows), 1) * 18)
        if y - required_height < bottom:
            pdf.showPage()
            y = A4[1] - (16 * mm)

        pdf.setFillColor(colors.HexColor("#16324f"))
        pdf.setFont(self.font_name, 12)
        pdf.drawString(left, y, title)
        y -= 18

        columns = (
            ("Rank", 34),
            ("Name", 150),
            ("Score", 52),
            ("Accuracy", 58),
            ("Progress", 58),
        )
        x = left
        pdf.setFillColor(colors.HexColor("#e8f0ff"))
        pdf.rect(left, y - 14, right - left, 16, fill=1, stroke=0)
        pdf.setFillColor(colors.HexColor("#111827"))
        pdf.setFont(self.font_name, 9)
        for label, width in columns:
            pdf.drawString(x + 4, y - 3, label)
            x += width
        y -= 18

        body_rows = rows or [{"rank_display": "-", "full_name": "No data", "score": 0, "accuracy": 0, "progress_percent": 0}]
        for row in body_rows:
            if row.get("is_current_user"):
                pdf.setFillColor(colors.HexColor("#dbeafe"))
                pdf.rect(left, y - 12, right - left, 16, fill=1, stroke=0)
            pdf.setFillColor(colors.HexColor("#111827"))
            pdf.setFont(self.font_name, 8.8)
            values = (
                str(row.get("rank_display") or "-"),
                str(row.get("full_name") or "User")[:28],
                f"{float(row.get('score') or 0):.2f}",
                f"{float(row.get('accuracy') or 0):.2f}%",
                f"{float(row.get('progress_percent') or 0):.2f}%",
            )
            x = left
            for (label, width), value in zip(columns, values):
                pdf.drawString(x + 4, y, value)
                x += width
            y -= 18
        return y - 8

    def _register_font(self) -> None:
        if self._font_registered:
            return
        preferred = BASE_DIR / "fonts" / "NotoSansDevanagari-Regular.ttf"
        fallback = BASE_DIR / "fonts" / "NotoSans-Regular.ttf"
        font_path = preferred if preferred.exists() else fallback if fallback.exists() else None
        if font_path:
            pdfmetrics.registerFont(TTFont(self.font_name, str(font_path)))
            self._font_registered = True
            return
        self.font_name = "Helvetica"
        self._font_registered = True

    def _slugify(self, value: str) -> str:
        cleaned = sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
        return cleaned or "quiz_result"


web_quiz_pdf_service = WebQuizPdfService()
