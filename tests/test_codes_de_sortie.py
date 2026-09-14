"""Un reessai ne doit porter que sur ce qui peut reussir au coup suivant.

CE QUI EST ARRIVE. Le workflow de collecte retente trois fois, avec soixante
secondes d'attente : un garde-fou legitime contre un 502 passager ou une
coupure reseau de quelques secondes.

Du 5 au 15 septembre 2026, ce garde-fou s'est retourne. La cause de l'echec
etait un VERDICT rendu par un test dbt sur des donnees deja ecrites dans le
lac : identique a chaque essai, par construction. Les trois tentatives ont
re-collecte trois snapshots, depense trois fois les credits OpenSky, et abouti
au meme echec. Treize minutes par execution, une execution par heure, dix
jours durant.

Le workflow ne pouvait pas faire mieux : il voyait un code de sortie non nul,
sans rien pour distinguer « le reseau a hoquete » de « la donnee est ce
qu'elle est ». Cette distinction existe maintenant, et ce fichier la tient des
deux cotes - le code qui la produit, et le workflow qui doit l'honorer. Un
code de sortie que personne ne lit ne corrige rien.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from skytrace import cli

COLLECT_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "collect.yml"


def _ecrire_compte_rendu(dossier: Path, resultats: list[dict]) -> Path:
    cible = dossier / "target"
    cible.mkdir(parents=True, exist_ok=True)
    fichier = cible / "run_results.json"
    fichier.write_text(json.dumps({"results": resultats}), encoding="utf-8")
    return fichier


class _Reglages:
    """Le strict necessaire : la lecture du compte rendu ne touche rien d'autre."""

    def __init__(self, dossier: Path) -> None:
        self.dbt_project_dir = dossier


def test_un_test_en_echec_est_reconnu_comme_verdict(tmp_path):
    """`fail` porte sur des donnees deja ecrites : le rejouer rendra le meme."""
    _ecrire_compte_rendu(
        tmp_path,
        [
            {"status": "pass", "unique_id": "test.skytrace.not_null_latitude.aaa"},
            {
                "status": "fail",
                "unique_id": "test.skytrace.accepted_range_heading_deg__360__0.bbb",
            },
        ],
    )

    assert cli._verdicts_de_qualite(_Reglages(tmp_path)) == ["accepted_range_heading_deg__360__0"]


def test_une_erreur_dexecution_nest_pas_un_verdict(tmp_path):
    """`error` signale une execution qui n'a pas abouti : elle peut aboutir ensuite.

    Confondre les deux couterait cher dans les deux sens : on cesserait de
    reessayer une coupure reseau, ou on continuerait a reessayer une donnee
    aberrante. dbt rend 1 dans les deux cas, d'ou la lecture du compte rendu.
    """
    _ecrire_compte_rendu(
        tmp_path,
        [{"status": "error", "unique_id": "model.skytrace.fct_aircraft_positions"}],
    )

    assert cli._verdicts_de_qualite(_Reglages(tmp_path)) == []


def test_un_compte_rendu_absent_ne_conclut_rien(tmp_path):
    """Le fichier est efface avant chaque build : son absence ne prouve rien.

    Un build qui echoue avant d'en ecrire un (lac injoignable, erreur de
    compilation) doit rester traite comme une cause possiblement passagere. En
    conclure « aucun verdict, donc rien de deterministe » est correct ; en
    conclure l'inverse arreterait les reessais quand ils servent.
    """
    assert cli._verdicts_de_qualite(_Reglages(tmp_path)) == []


def test_un_compte_rendu_illisible_ne_fait_pas_tomber_le_pipeline(tmp_path):
    """Un diagnostic ne doit pas pouvoir aggraver la panne qu'il decrit."""
    cible = tmp_path / "target"
    cible.mkdir(parents=True)
    (cible / "run_results.json").write_text("{ ceci n'est pas du json", encoding="utf-8")

    assert cli._verdicts_de_qualite(_Reglages(tmp_path)) == []


def test_le_pipeline_annonce_un_verdict_par_son_code_de_sortie(tmp_path, monkeypatch):
    """Le bout qui compte : le code de sortie doit porter la distinction.

    Sans lui, le workflow voit « non nul » et rejoue - ce qui a produit dix
    jours de pannes horaires.
    """
    reglages = _Reglages(tmp_path)
    _ecrire_compte_rendu(
        tmp_path, [{"status": "fail", "unique_id": "test.skytrace.accepted_range_cap.ccc"}]
    )

    monkeypatch.setattr(cli, "get_settings", lambda: reglages)
    monkeypatch.setattr(cli, "cmd_ingest_states", lambda _a: 0)
    monkeypatch.setattr(cli, "_maybe_refresh_air_quality", lambda _s: None)
    monkeypatch.setattr(cli, "_maybe_refresh_fleet", lambda _s: None)
    monkeypatch.setattr("skytrace.storage.object_age_seconds", lambda *_a, **_k: 0)
    monkeypatch.setattr("skytrace.storage.prune_old_states", lambda *_a, **_k: None)

    def dbt_en_echec(_args):
        """Un vrai build en echec ecrit son compte rendu AVANT de rendre la main.

        Le pipeline efface le precedent juste avant : reecrire ici reproduit
        fidelement ce que dbt laisse derriere lui, et verifie au passage que
        l'effacement ne detruit pas le diagnostic du build courant.
        """
        _ecrire_compte_rendu(
            tmp_path, [{"status": "fail", "unique_id": "test.skytrace.accepted_range_cap.ccc"}]
        )
        return 1

    monkeypatch.setattr(cli, "cmd_dbt", dbt_en_echec)

    code = cli.cmd_pipeline(argparse.Namespace(with_reference=False))

    assert code == cli.CODE_VERDICT_QUALITE


def test_une_panne_passagere_garde_son_code(tmp_path, monkeypatch):
    """Sans verdict, le pipeline rend le code de dbt : le reessai reste possible."""
    reglages = _Reglages(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: reglages)
    monkeypatch.setattr(cli, "cmd_ingest_states", lambda _a: 0)
    monkeypatch.setattr(cli, "_maybe_refresh_air_quality", lambda _s: None)
    monkeypatch.setattr(cli, "_maybe_refresh_fleet", lambda _s: None)
    monkeypatch.setattr("skytrace.storage.object_age_seconds", lambda *_a, **_k: 0)
    monkeypatch.setattr("skytrace.storage.prune_old_states", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "cmd_dbt", lambda _a: 1)

    code = cli.cmd_pipeline(argparse.Namespace(with_reference=False))

    assert code == cli.CODE_ECHEC_PASSAGER


def test_les_codes_sont_distincts():
    """Deux causes confondues dans un meme code redeviendraient indiscernables."""
    codes = [cli.CODE_OK, cli.CODE_ECHEC_PASSAGER, cli.CODE_QUOTA_EPUISE, cli.CODE_VERDICT_QUALITE]

    assert len(set(codes)) == len(codes)


# -- le workflow doit honorer ces codes --------------------------------------
# Un code de sortie que le workflow ignore ne corrige rien : c'est precisement
# la situation d'avant, ou la seule information disponible etait « non nul ».


def _etape_de_collecte() -> str:
    texte = COLLECT_YML.read_text(encoding="utf-8")
    debut = texte.index("Collecte et transformation")
    fin = texte.index("Publication de l'etat du pipeline", debut)
    return texte[debut:fin]


def test_le_workflow_lit_le_code_de_sortie():
    """Le shell d'Actions tourne avec `bash -e` : sans `set +e`, le code est perdu.

    L'idiome compte autant que l'intention. `bash -e` interrompt le script des
    la premiere commande en echec, donc la ligne qui releve `$?` ne serait
    jamais atteinte, et toute la distinction serait inerte sans que rien ne le
    signale.
    """
    etape = _etape_de_collecte()

    assert "set +e" in etape, "sans `set +e`, `bash -e` interrompt avant la lecture du code"
    assert re.search(r"code=\$\?", etape), "le code de sortie n'est jamais releve"


@pytest.mark.parametrize(
    ("code", "cause"),
    [("2", "quota epuise"), ("3", "verdict de qualite")],
)
def test_le_workflow_ne_rejoue_pas_une_cause_deterministe(code: str, cause: str):
    """Les deux causes que rejouer ne peut pas resoudre doivent sortir tout de suite."""
    etape = _etape_de_collecte()
    bloc = re.search(rf"^\s*{code}\)\s*$.*?;;", etape, re.MULTILINE | re.DOTALL)

    assert bloc, f"le workflow ne traite pas le code {code} ({cause}) a part"
    assert "exit 1" in bloc.group(), f"le code {code} doit echouer sans reessayer"


def test_le_workflow_reessaie_toujours_ce_qui_peut_reussir():
    """La correction ne doit pas supprimer le garde-fou qu'elle affine.

    Un 502 passager ou une coupure de quelques secondes restent la raison
    d'etre des trois tentatives : les retirer echangerait un defaut contre un
    autre.
    """
    etape = _etape_de_collecte()

    assert "sleep 60" in etape, "l'attente entre tentatives a disparu"
    assert "for tentative in 1 2 3" in etape, "les trois tentatives ont disparu"
