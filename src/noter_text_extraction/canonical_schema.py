"""Canonical Norwegian regnskap schema.

Mirrors the flat structure of `data.brreg.no/regnskapsregisteret/regnskap/{orgnr}`.
Every field carries a Norwegian label set, an extraction layout tag (where on the
PDF the value is expected to appear), and a tolerance class for nokkeltall validation.

Layout tags:
    generell    page 1, KV grid
    resultat    resultatregnskap pages, row-label + 2-year columns
    balanse     balanse pages, row-label + 2-year columns

Tolerance classes:
    exact       integer match required
    pct1        within 1% relative or 1000 NOK absolute, whichever larger
    bool_match  must match exactly
    str_match   string equality
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    canonical: str
    api_path: str
    layout: str
    norwegian_labels: tuple[str, ...]
    tolerance: str = "pct1"
    optional: bool = False


META_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("orgnr", "virksomhet.organisasjonsnummer", "generell",
              ("Organisasjonsnummer", "Org.nr", "Foretakets organisasjonsnummer"),
              tolerance="exact"),
    FieldSpec("foretaksnavn", "virksomhet.navn", "generell",
              ("Foretaksnavn", "Selskapsnavn", "Navn"),
              tolerance="str_match", optional=True),
    FieldSpec("organisasjonsform", "virksomhet.organisasjonsform", "generell",
              ("Organisasjonsform",),
              tolerance="str_match", optional=True),
    FieldSpec("regnskapstype", "regnskapstype", "generell",
              ("Regnskapstype",),
              tolerance="str_match", optional=True),
    FieldSpec("regnskapsperiode_fra", "regnskapsperiode.fraDato", "generell",
              ("Regnskapsperiode", "Fra dato", "Periode"),
              tolerance="str_match", optional=True),
    FieldSpec("regnskapsperiode_til", "regnskapsperiode.tilDato", "generell",
              ("Til dato",),
              tolerance="str_match", optional=True),
    FieldSpec("valuta", "valuta", "generell",
              ("Valuta",),
              tolerance="str_match", optional=True),
    FieldSpec("oppstillingsplan", "oppstillingsplan", "generell",
              ("Oppstillingsplan",),
              tolerance="str_match", optional=True),
    FieldSpec("smaa_foretak", "regnkapsprinsipper.smaaForetak", "generell",
              ("Regnskapet er utarbeidet i samsvar med regnskapsregler for små foretak",
               "Små foretak", "Regler for små foretak"),
              tolerance="bool_match", optional=True),
    FieldSpec("morselskap", "virksomhet.morselskap", "generell",
              ("Morselskap i konsern", "Morselskap"),
              tolerance="bool_match", optional=True),
    FieldSpec("avviklingsregnskap", "avviklingsregnskap", "generell",
              ("Avviklingsregnskap",),
              tolerance="bool_match", optional=True),
    FieldSpec("ikke_revidert", "revisjon.ikkeRevidertAarsregnskap", "generell",
              ("Ikke revidert årsregnskap",),
              tolerance="bool_match", optional=True),
    FieldSpec("fravalg_revisjon", "revisjon.fravalgRevisjon", "generell",
              ("Fravalg av revisjon",),
              tolerance="bool_match", optional=True),
    FieldSpec("regnskapsregler", "regnkapsprinsipper.regnskapsregler", "generell",
              ("Regnskapsregler",),
              tolerance="str_match", optional=True),
    FieldSpec("journalnr", "journalnr", "generell",
              ("Journalnr", "Journalnummer"),
              tolerance="exact", optional=True),
)


# Resultatregnskap (income statement) — leaf numerical fields
RESULTAT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("sum_driftsinntekter",
              "resultatregnskapResultat.driftsresultat.driftsinntekter.sumDriftsinntekter",
              "resultat",
              ("Sum driftsinntekter", "Sum inntekter")),
    FieldSpec("sum_driftskostnad",
              "resultatregnskapResultat.driftsresultat.driftskostnad.sumDriftskostnad",
              "resultat",
              ("Sum driftskostnader", "Sum driftskostnad", "Sum kostnader")),
    FieldSpec("driftsresultat",
              "resultatregnskapResultat.driftsresultat.driftsresultat",
              "resultat",
              ("Driftsresultat",)),
    FieldSpec("sum_finansinntekter",
              "resultatregnskapResultat.finansresultat.finansinntekt.sumFinansinntekter",
              "resultat",
              ("Sum finansinntekter",)),
    FieldSpec("sum_finanskostnad",
              "resultatregnskapResultat.finansresultat.finanskostnad.sumFinanskostnad",
              "resultat",
              ("Sum finanskostnader",)),
    FieldSpec("netto_finans",
              "resultatregnskapResultat.finansresultat.nettoFinans",
              "resultat",
              ("Netto finansposter", "Sum netto finansposter",
               "Resultat av finansposter", "Netto finans")),
    FieldSpec("ordinaert_resultat_for_skatt",
              "resultatregnskapResultat.ordinaertResultatFoerSkattekostnad",
              "resultat",
              ("Ordinært resultat før skattekostnad",
               "Resultat før skattekostnad",
               "Ordinært resultat før skatt",
               "Resultat før skatt")),
    FieldSpec("aarsresultat",
              "resultatregnskapResultat.aarsresultat",
              "resultat",
              ("Årsresultat",)),
    FieldSpec("totalresultat",
              "resultatregnskapResultat.totalresultat",
              "resultat",
              ("Totalresultat",), optional=True),
)


# Balanse (balance sheet)
BALANSE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("sum_anleggsmidler",
              "eiendeler.anleggsmidler.sumAnleggsmidler",
              "balanse",
              ("Sum anleggsmidler",)),
    FieldSpec("sum_omloepsmidler",
              "eiendeler.omloepsmidler.sumOmloepsmidler",
              "balanse",
              ("Sum omløpsmidler", "Sum omloepsmidler")),
    FieldSpec("sum_eiendeler",
              "eiendeler.sumEiendeler",
              "balanse",
              ("Sum eiendeler", "SUM EIENDELER")),
    FieldSpec("sum_innskutt_egenkapital",
              "egenkapitalGjeld.egenkapital.innskuttEgenkapital.sumInnskuttEgenkaptial",
              "balanse",
              ("Sum innskutt egenkapital",)),
    FieldSpec("sum_opptjent_egenkapital",
              "egenkapitalGjeld.egenkapital.opptjentEgenkapital.sumOpptjentEgenkapital",
              "balanse",
              ("Sum opptjent egenkapital",)),
    FieldSpec("sum_egenkapital",
              "egenkapitalGjeld.egenkapital.sumEgenkapital",
              "balanse",
              ("Sum egenkapital",)),
    FieldSpec("sum_langsiktig_gjeld",
              "egenkapitalGjeld.gjeldOversikt.langsiktigGjeld.sumLangsiktigGjeld",
              "balanse",
              ("Sum langsiktig gjeld",), optional=True),
    FieldSpec("sum_kortsiktig_gjeld",
              "egenkapitalGjeld.gjeldOversikt.kortsiktigGjeld.sumKortsiktigGjeld",
              "balanse",
              ("Sum kortsiktig gjeld",), optional=True),
    FieldSpec("sum_gjeld",
              "egenkapitalGjeld.gjeldOversikt.sumGjeld",
              "balanse",
              ("Sum gjeld",)),
    FieldSpec("sum_egenkapital_gjeld",
              "egenkapitalGjeld.sumEgenkapitalGjeld",
              "balanse",
              ("Sum egenkapital og gjeld", "SUM EGENKAPITAL OG GJELD",
               "Sum gjeld og egenkapital")),
)


ALL_FIELDS = META_FIELDS + RESULTAT_FIELDS + BALANSE_FIELDS

CANONICAL_FIELD_NAMES = tuple(f.canonical for f in ALL_FIELDS)


def get_by_layout(layout: str) -> tuple[FieldSpec, ...]:
    return tuple(f for f in ALL_FIELDS if f.layout == layout)


def get_field(canonical: str) -> FieldSpec | None:
    for f in ALL_FIELDS:
        if f.canonical == canonical:
            return f
    return None


def flatten_api(api_entry: dict) -> dict:
    """Convert nested regnskapsapi entry to flat canonical dict."""
    out = {}
    for spec in ALL_FIELDS:
        cur = api_entry
        for part in spec.api_path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        out[spec.canonical] = cur
    return out
