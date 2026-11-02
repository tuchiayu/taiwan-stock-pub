"""Display official industry names without rewriting stored source codes.

Sources: TWSE transmission specification B.12.11, appendix 3, and TPEx
after-hours transmission specification V1.33, appendix 1 (checked 2026-10-16):
https://dsp.twse.com.tw/public/static/downloads/computerPlanningOperationsDepartment/TWSE集中市場即時交易資訊傳輸規格書(B.12.11)(202503)_20250113092444.pdf
https://www.tpex.org.tw/storage/regular_system/新版收市後交易資訊格式說明(V1.33版).pdf
"""

TWSE = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險",
    "18": "貿易百貨",
    "19": "綜合",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子",
    "35": "綠能環保",
    "36": "數位雲端",
    "37": "運動休閒",
    "38": "居家生活",
}
TPEX = {
    **{k: v for k, v in TWSE.items() if k not in {"01", "09", "12", "18", "19"}},
    "17": "金融業",
    "22": "生技醫療",
    "31": "其他電子業",
    "32": "文化創意業",
    "33": "農業科技",
    "80": "管理股票",
}


def industry_name(code: str | None, market: str | None = None) -> str:
    """Unknown markets/codes keep the source value visible, rather than guessing."""
    if not code:
        return "—"
    names = {"TWSE": TWSE, "TPEx": TPEX}.get(market, {})
    return names.get(code, code)


def industry_filter_label(code: str) -> str:
    """A filter may cover both markets, whose labels sometimes differ."""
    names = list(dict.fromkeys(m[code] for m in (TWSE, TPEX) if code in m))
    return f"{'／'.join(names)}（{code}）" if names else code
