"""
LLM-based verification judge.

Accepts company info + website text, returns structured verdict.
Provider-agnostic: swap the implementation to change LLM backend.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


GOJI_RE = re.compile(
    r"\b(goji|goji berry|goji berries|wolfberry|wolfberries|lycium barbarum|"
    r"lycium chinense|goji powder|goji extract|dried goji|organic goji|"
    r"kurt üzümü|kurt uzumu|kustovnice)\b|枸杞|годжи|годж",
    re.IGNORECASE,
)
CN_ENTITY_RE = re.compile(
    r"\b(china|p\.?\s*r\.?\s*china|p\.?r\.?c\.?|"
    r"dongguan|ningxia|yiwu|guangzhou|shanghai|zhejiang|shaanxi|"
    r"hangzhou|qinghai|gansu|beijing|shenzhen|jiangsu|anhui|"
    r"henan|shandong|hunan|hubei|sichuan|guangxi|xian)\b|"
    r"中国|东莞|宁夏|义乌|广州|上海|浙江|陕西|杭州|青海|甘肃|"
    r"北京|深圳|江苏|安徽|河南|山东|湖南|湖北|四川|广西|西安",
    re.IGNORECASE,
)
CN_SUPPLIER_BIZ_RE = re.compile(
    r"\b(manufacturer|manufacturing|producer|production|extractor|extraction|"
    r"exporter|export|oem|odm|supplier|factory|plantation|grower|"
    r"contract\s+manufactur)\b",
    re.IGNORECASE,
)


@dataclass
class JudgeInput:
    company_name: str
    country: str
    domain: str
    lead_type: str
    website_text: str


@dataclass
class JudgeVerdict:
    b2b_or_b2c: str
    is_target: str
    customer_type: str
    is_competitor: bool
    goji_presence: str
    rating: str
    p_priority: str
    track_match: str
    matched_track: str
    evidence_url: str
    primary_vertical: str
    food_supplement_focus: str
    rating_reason: str
    outreach_angle: str
    website_country: str


SYSTEM_PROMPT = """
You are a verification analyst for goji berry export customer development.
Return ONLY valid JSON matching the requested schema. Do not include markdown.

Task:
1. Judge whether the company is B2B, B2C, or Both.
2. Judge whether it is a target customer for goji ingredient export.
3. Detect competitors and exclude them from target status.
4. Detect goji/wolfberry/lycium presence from website evidence.
5. Evaluate functional track fit and assign S/A/B/P/Z rating.

B2B/B2C rules:
- B2B signals: wholesale, bulk, manufacturer, importer, distributor, ingredient supplier,
  contract manufacturing, OEM, private label, trade inquiry, MOQ.
- B2C signals: shop now, add to cart, retail prices, direct consumer supplements/foods.
- If both signals exist, output "Both".

Target customer types (判定基于主营品类是否与枸杞相邻；不要求当前已售枸杞——已售枸杞通过 goji_presence=Yes 和 rating=S 单独表达):
- 原料分销商: B2B distributor / wholesaler / importer of food, supplement, herbal, botanical,
  dried fruit, nuts, tea, superfood, or other adjacent raw materials. Even if the company
  does not yet sell goji, an ingredient-side distributor in adjacent categories is a target.
- OEM制造商: contract manufacturer, private label, or OEM in food, supplement, nutraceutical,
  cosmetic, or herbal formulation. Goji is not required.
- 品牌商: brand or B2C retailer of finished products in food, beverage, tea, supplement,
  nutraceutical, herbal medicine, traditional medicine, healthy/organic food,
  superfood, or cosmetic categories. Tea brands, dried-fruit brands, herbal-medicine
  shops, organic-food retailers, and beauty/skincare brands all qualify, even if no
  goji SKU is currently listed.
- 竞争对手: own goji cultivation/farm/plantation, raw goji producer/extractor,
  China-based goji exporter.
- 不相关: 主营与食品/补剂/草本/化妆品/茶/干果/有机生活方式全无关联——例如电子产品、家电、
  服装鞋帽、运动器材、汽车、建材、纯服务业、广告、旅游、电信、媒体。仅对真正无品类邻接的公司使用。

Competitor rules:
- If the company produces, farms, extracts, or exports goji as its own raw ingredient,
  mark is_competitor=true, is_target="No", customer_type="竞争对手".
- A brand/distributor that sells goji as a product is not automatically a competitor unless
  it appears to be the producer/source exporter.

Goji keywords:
goji, goji berry, goji berries, wolfberry, wolfberries, lycium barbarum,
lycium chinense, goji powder, goji extract, dried goji, organic goji, 枸杞.
If these appear in concrete product/catalog/formula evidence, Goji Presence should be "Yes".
If no evidence after checking available website text, use "No".
If the website is too thin or inaccessible, use "Unclear".

Website-based country judgement:
- Determine the company's headquarters country strictly from website evidence:
  footer copyright, "About us" address, "Contact" address, and explicit
  legal entity name. Domain TLD is only a weak hint, never the sole basis.
- Output English country name (e.g., "Germany", "United States", "China",
  "United Kingdom", "Italy"). If website text is too thin to decide, output
  "Unclear". Do not output city or region.

Primary vertical:
- supplement: dietary supplement / nutraceutical / functional food brand.
- food_beverage: food, beverage, snack, tea, herbal infusion brand or distributor.
- herbal_medicine: TCM / Ayurveda / herbal pharmacy / botanical medicine.
- cosmetic_beauty: skincare / cosmetic / beauty.
- fitness_equipment: sports gear / activewear / fitness apparel / training equipment.
- general_marketplace: broad-category marketplace where food or supplements are a slice.
- agriculture_raw: farm produce, fresh fruit, raw nuts, or non-processed agriculture.
- other: use only if none of the above fit.
Judge primary_vertical by homepage navigation, first-level categories, and About positioning.
Do not lower rating only because the primary vertical is broad or costly to sell into.

Food/supplement focus:
- core: food, supplements, herbs, or botanicals are the main business, >60% SKU or marketing focus.
- partial: food/supplement is a meaningful category, about 20-60%.
- marginal: food/supplement exists but is a small edge category, <20%.
- none: no food/supplement category.
Use first-level navigation, homepage promoted sections, and product keyword share.

Functional tracks for Potential Buyer:
- 护眼/眼健康: eye health, lutein, zeaxanthin, macular, vision support, blue light.
- 抗氧化/抗衰老: antioxidant, anti-aging, longevity, oxidative stress, free radical, ORAC.
- 超级浆果配方: superfruit, mixed berry, berry blend, acai, elderberry, bilberry.
- 免疫支持: immune support, immunity, immune health.
- 护肝/肝健康: liver health, liver support, hepatoprotective, detox liver, betaine.
- TCM/传统草本: TCM, traditional Chinese medicine, Chinese herbal, wolfberry formula.
- 美容养颜: beauty supplement, skin health, collagen, radiance, glow skin.
- 男性健康: men's health, male vitality, testosterone, fertility, zinc selenium.
Track Match:
- "强匹配": the track is a core product line or clear flagship claim.
- "弱匹配": only broad/edge relevance or generic health product fit.
- "无匹配": no relevant track signal.
- "N/A": Direct Buyer/OEM where track logic is not applicable.

Rating matrix:
- S: concrete website evidence already shows goji/wolfberry/lycium product or formula.
- A: target company, no goji found, but business/product catalog is relevant.
  Adjacent-category brands (tea, dried fruit, herbal, cosmetic, organic food)
  with primary_vertical in {food_beverage, herbal_medicine, cosmetic_beauty}
  and food_supplement_focus in {core, partial} should land at A unless the
  website is too thin to verify.
- B: insufficient info, thin/inaccessible website, or cannot verify.
- P: only for terminal brands, no goji found, strong functional track match and goji has a plausible formula angle.
- Z: excluded record, especially direct competitors that should not receive sales outreach.
P priority:
- P1: strong eye health / antioxidant / superfruit track, adjacent berries or clear R&D/procurement angle.
- P2: track fits but not core, information is partial, or purchase chain likely complex.
- P3: weak track fit or long-term watchlist.
For non-P ratings, p_priority must be "".

Output JSON keys:
b2b_or_b2c, is_target, customer_type, is_competitor, goji_presence, rating,
p_priority, track_match, matched_track, evidence_url, rating_reason,
primary_vertical, food_supplement_focus, outreach_angle, website_country.

Allowed values:
b2b_or_b2c: "B2B" | "B2C" | "Both"
is_target: "Yes" | "No" | "Unclear"
customer_type: "原料分销商" | "OEM制造商" | "品牌商" | "竞争对手" | "不相关"
goji_presence: "Yes" | "No" | "Unclear"
rating: "S" | "A" | "B" | "P" | "Z"
p_priority: "P1" | "P2" | "P3" | ""
track_match: "强匹配" | "弱匹配" | "无匹配" | "N/A"
primary_vertical: "supplement" | "food_beverage" | "herbal_medicine" | "cosmetic_beauty" | "fitness_equipment" | "general_marketplace" | "agriculture_raw" | "other"
food_supplement_focus: "core" | "partial" | "marginal" | "none"
rating_reason: Chinese, <=60 chars.
outreach_angle: Chinese, <=40 chars.
website_country: English country name or "Unclear"
""".strip()


def _json_loads_loose(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _coerce_verdict(data: dict) -> JudgeVerdict:
    verdict = JudgeVerdict(
        b2b_or_b2c=str(data.get("b2b_or_b2c") or "B2B"),
        is_target=str(data.get("is_target") or "Unclear"),
        customer_type=str(data.get("customer_type") or "不相关"),
        is_competitor=bool(data.get("is_competitor", False)),
        goji_presence=str(data.get("goji_presence") or "Unclear"),
        rating=str(data.get("rating") or "B"),
        p_priority=str(data.get("p_priority") or ""),
        track_match=str(data.get("track_match") or "N/A"),
        matched_track=str(data.get("matched_track") or ""),
        evidence_url=str(data.get("evidence_url") or ""),
        primary_vertical=str(data.get("primary_vertical") or "other"),
        food_supplement_focus=str(data.get("food_supplement_focus") or "none"),
        rating_reason=str(data.get("rating_reason") or "")[:60],
        outreach_angle=str(data.get("outreach_angle") or "")[:40],
        website_country=str(data.get("website_country") or "Unclear"),
    )
    # Hard invariant: competitor cannot also be a target customer.
    if verdict.is_competitor:
        verdict.customer_type = "竞争对手"
        verdict.is_target = "No"
        verdict.rating = "Z"
        verdict.p_priority = ""
    # Non-P ratings must not carry a P priority.
    if verdict.rating != "P":
        verdict.p_priority = ""
    return verdict


def _is_cn_local_supplier(input: JudgeInput) -> bool:
    """Detect China-headquartered supplier/manufacturer records to exclude before LLM."""
    corpus = " ".join(
        [
            input.company_name or "",
            input.country or "",
            input.domain or "",
            (input.website_text or "")[:8000],
        ]
    )
    return bool(CN_ENTITY_RE.search(corpus) and CN_SUPPLIER_BIZ_RE.search(corpus))


def _cn_supplier_verdict(input: JudgeInput) -> JudgeVerdict:
    goji_hint = "Yes" if GOJI_RE.search(input.website_text or "") else "No"
    return JudgeVerdict(
        b2b_or_b2c="B2B",
        is_target="No",
        customer_type="不相关",
        is_competitor=False,
        goji_presence=goji_hint,
        rating="Z",
        p_priority="",
        track_match="N/A",
        matched_track="",
        evidence_url="",
        primary_vertical="other",
        food_supplement_focus="none",
        rating_reason="中国本土供应商，非出海买家目标",
        outreach_angle="",
        website_country="China",
    )


def judge(input: JudgeInput) -> JudgeVerdict:
    """Call LLM and return a structured verification verdict."""
    if _is_cn_local_supplier(input):
        return _cn_supplier_verdict(input)

    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GLM_API_KEY")
    )
    if not api_key:
        raise RuntimeError("LLM_API_KEY is empty. Set it in .env or pass --skip-step4.")

    # Lazy import keeps non-step4 commands usable before dependencies are installed.
    from openai import OpenAI

    goji_hint = "Yes" if GOJI_RE.search(input.website_text or "") else "No"
    user_payload = {
        "company_name": input.company_name,
        "country": input.country,
        "domain": input.domain,
        "lead_type": input.lead_type,
        "regex_goji_hint": goji_hint,
        "website_text": input.website_text[:16000],
    }

    client_kwargs: dict = {"api_key": api_key}
    base_url = os.environ.get("LLM_BASE_URL")
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content or "{}"
    verdict = _coerce_verdict(_json_loads_loose(content))
    if goji_hint == "Yes":
        verdict.goji_presence = "Yes"
        if not verdict.is_competitor and verdict.rating in {"A", "B", "P"}:
            verdict.rating = "S"
    return verdict
