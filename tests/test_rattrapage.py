"""Le rattrapage doit combler les trous, ou s'abstenir en le disant.

Mesure sur ce depot au moment de l'ecriture : 102 heures perdues sur environ
250, dont un trou de 35 heures pendant que l'ordonnanceur GitHub se taisait.
Ces positions ne revenaient jamais : la collecte reprend au present et la
serie garde son vide pour toujours.

Deux proprietes comptent ici, et la seconde autant que la premiere :
  * combler les vrais trous, sans rejouer ce qui existe deja ;
  * ne JAMAIS faire echouer une tache planifiee quand la capacite n'est pas
    configuree - une alerte qui ne demande aucune action apprend a ignorer
    les suivantes.
"""

from __future__ import annotations

import time

import pytest

from skytrace.ingestion import states as mod


class FauxReglages:
    uses_r2 = False

    def __init__(self, dossier):
        self.states_dir = dossier

    def ensure_directories(self):
        pass


@pytest.fixture
def lac(tmp_path, monkeypatch):
    """Un lac local dont on choisit les instants de releve."""
    dossier = tmp_path / "states"
    dossier.mkdir()
    reglages = FauxReglages(dossier)
    monkeypatch.setattr(mod, "get_settings", lambda: reglages)
    return dossier


def deposer(dossier, instants):
    for t in instants:
        (dossier / f"states_{int(t)}.parquet").write_bytes(b"x")


class TestDetectionDesTrous:
    def test_une_serie_reguliere_n_a_pas_de_trou(self, lac):
        maintenant = time.time()
        deposer(lac, [maintenant - h * 3600 for h in range(12)])
        assert mod.find_gaps(min_gap_hours=3.0) == []

    def test_un_vrai_trou_est_repere(self, lac):
        """Le cas reel : 35 heures sans le moindre releve."""
        maintenant = time.time()
        deposer(lac, [maintenant - 1800, maintenant - 36 * 3600, maintenant - 37 * 3600])
        trous = mod.find_gaps(min_gap_hours=3.0)
        assert len(trous) == 1
        debut, fin = trous[0]
        assert 35 < (fin - debut) / 3600 < 37

    def test_un_retard_ordinaire_n_est_pas_un_trou(self, lac):
        """Le cron GitHub n'est pas ponctuel : deux heures d'ecart sont normales."""
        maintenant = time.time()
        deposer(lac, [maintenant - 1800, maintenant - 2 * 3600 - 1800])
        assert mod.find_gaps(min_gap_hours=3.0) == []

    def test_l_horizon_borne_le_rattrapage_sans_masquer_le_trou(self, lac):
        """Remonter indefiniment couterait des credits pour des heures oubliees.

        Le trou reste signale - il existe - mais son debut est ramene a
        l'horizon : on ne rejoue pas quatre cents heures pour une serie que
        plus personne ne comparera si loin.
        """
        maintenant = time.time()
        deposer(lac, [maintenant - 1800, maintenant - 400 * 3600])
        ((debut, fin),) = mod.find_gaps(min_gap_hours=3.0, horizon_hours=48)
        assert (fin - debut) / 3600 <= 48

    def test_un_lac_vide_ne_leve_pas(self, lac):
        assert mod.find_gaps() == []


class TestAbstention:
    def test_sans_identifiants_le_rattrapage_s_abstient(self, lac, monkeypatch):
        """LE test qui protege des fausses alertes.

        L'API OpenSky refuse l'historique aux requetes anonymes. La commande
        doit le constater et se taire, jamais echouer : une tache planifiee
        qui rougit parce qu'une capacite optionnelle n'est pas configuree
        envoie un courriel que personne ne peut traiter.
        """

        class ClientAnonyme:
            anonymous = True

            def close(self):
                pass

        monkeypatch.setattr(mod, "OpenSkyClient", lambda *_a, **_k: ClientAnonyme())

        resultat = mod.backfill_gaps()

        assert resultat.snapshots_written == 0
        assert resultat.skipped_reason is not None
        assert "OPENSKY_CLIENT_ID" in resultat.skipped_reason

    def test_la_raison_nomme_ce_qu_il_faut_definir(self, lac, monkeypatch):
        class ClientAnonyme:
            anonymous = True

            def close(self):
                pass

        monkeypatch.setattr(mod, "OpenSkyClient", lambda *_a, **_k: ClientAnonyme())
        raison = mod.backfill_gaps().skipped_reason
        assert "OPENSKY_CLIENT_SECRET" in raison


class TestPlafondDeDepense:
    def test_le_nombre_de_releves_rattrapes_est_borne(self, lac, monkeypatch):
        """Un trou de plusieurs jours ne doit pas vider le budget d'un coup."""
        maintenant = time.time()
        deposer(lac, [maintenant - 1800, maintenant - 100 * 3600])

        appels = []

        class ClientAuthentifie:
            anonymous = False

            def get_states(self, *, at):
                appels.append(at)
                return mod.StatesSnapshot(
                    snapshot_ts=at, region="world", vectors=[], credits_spent=4
                )

            def close(self):
                pass

        monkeypatch.setattr(mod, "OpenSkyClient", lambda *_a, **_k: ClientAuthentifie())

        mod.backfill_gaps(max_snapshots=5, min_gap_hours=3.0)

        assert len(appels) <= 5, "le plafond de depense doit etre respecte"


class TestFenetreHistorique:
    """OpenSky ne sert d'historique que sur une heure. Mesure, pas suppose.

    Le rattrapage a d'abord ete ecrit en supposant qu'un compte authentifie
    ouvrait l'historique. La mesure a dit le contraire :

        t-55 min  200
        t-65 min  403  "can only be retrieved with /states/own"

    Au-dela d'une heure, il faut alimenter le reseau avec son propre
    recepteur. Les trous de plusieurs heures sont donc DEFINITIFS, et le code
    ne doit pas depenser de credits a redemander ce qui sera refuse.
    """

    @staticmethod
    def _client_qui_refuse_tout_appel(monkeypatch, appels):
        class ClientAuthentifie:
            anonymous = False

            def get_states(self, *, at):
                appels.append(at)
                raise AssertionError("aucun appel ne doit partir hors fenetre")

            def close(self):
                pass

        monkeypatch.setattr(mod, "OpenSkyClient", lambda *_a, **_k: ClientAuthentifie())

    def test_aucun_credit_depense_hors_fenetre(self, lac, monkeypatch):
        """LE test qui evite de bruler des credits pour un 403 connu d'avance."""
        maintenant = time.time()
        deposer(lac, [maintenant - 1800, maintenant - 40 * 3600])
        appels = []
        self._client_qui_refuse_tout_appel(monkeypatch, appels)

        mod.backfill_gaps(min_gap_hours=3.0)

        assert appels == [], "un instant hors fenetre recevrait un 403 paye"

    def test_un_trou_entierement_ancien_dit_qu_il_est_perdu(self, lac, monkeypatch):
        """Le silence serait pire : l'utilisateur attendrait un rattrapage."""
        maintenant = time.time()
        deposer(lac, [maintenant - 40 * 3600, maintenant - 100 * 3600])
        appels = []
        self._client_qui_refuse_tout_appel(monkeypatch, appels)

        resultat = mod.backfill_gaps(min_gap_hours=3.0)

        assert appels == []
        assert "definitivement perdues" in resultat.skipped_reason

    def test_la_fenetre_documentee_vaut_une_heure(self):
        assert mod.HISTORICAL_WINDOW_SECONDS == 3600
