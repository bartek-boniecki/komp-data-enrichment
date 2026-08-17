"""Tests for Kompass project-page metadata parsing."""

from skysnap.kompass_project import (
    map_kompass_typ_to_hubspot,
    parse_kompass_project_meta,
    sector_hubspot_value,
)

_SAMPLE = """
Ogólne informacje
Przedmiotem inwestycji jest budowa magazynu zasobów ochrony ludności i obrony cywilnej w miejscowości Miejsce Piastowe.

Typ
Publiczna

Sektor, podsektor
niemieszkaniowy - budynki magazynowe, centra logistyczne

Województwo
podkarpackie

Powiat
krośnieński

Miasto
Miejsce Piastowe

Kod pocztowy
38-430

Adres
ul. Jaćmierz

Numery działek
Obręb: 180707_2.0003: działka nr 1340, 1342/1
"""


def test_parse_kompass_project_meta_amex_page():
    meta = parse_kompass_project_meta(_SAMPLE)
    assert meta.investment_type == "Publiczna"
    assert meta.sector_subsector == "niemieszkaniowy - budynki magazynowe, centra logistyczne"
    assert meta.city == "Miejsce Piastowe"
    assert meta.voivodeship == "podkarpackie"
    assert meta.street == "ul. Jaćmierz"
    assert meta.building_number is None
    assert meta.project_description and "magazynu zasobów" in meta.project_description


def test_map_kompass_typ_publiczna():
    assert map_kompass_typ_to_hubspot("Publiczna") == "publiczne"
    assert map_kompass_typ_to_hubspot("prywatna") == "prywatne"
    assert map_kompass_typ_to_hubspot("publiczno-prawne") == "publiczno-prawne"


def test_sector_hubspot_value_strips_prefix():
    assert (
        sector_hubspot_value("niemieszkaniowy - budynki magazynowe, centra logistyczne")
        == "budynki magazynowe, centra logistyczne"
    )
