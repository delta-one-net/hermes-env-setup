# Anforderung an den `hermes-agent`-Chart: Plugin-Bootstrap pro Tenant

**An:** Oliver Hart (Chart-Owner, Hermes Fleet)
**Von:** Nick Hechtel / Charis
**Scope:** einmalige Chart-Erweiterung (~20 Zeilen Template + 1 Value). Danach
verteilen sich Plugins automatisch auf alle bestehenden und künftigen Tenants.
**Bezug:** Outline-Runbook „New Tenant" (`new-tenant-rTK0iu91Zt`) — die
Plugin-Stufe fehlt dort komplett; diese Erweiterung schließt die Lücke, ohne
am Runbook-Ablauf (Bifrost → Infisical → HelmRelease) etwas zu ändern.

---

## TL;DR

Eine Chart-Value `bootstrapPlugins`, die beim Pod-Start einen **initContainer**
mit demselben Image gegen das persistente Data-Volume laufen lässt und pro
Eintrag aus `bootstrapPlugins.plugins` installiert. Beispiel:

```bash
hermes plugins install delta-one-net/hermes-env-setup --enable --yes-deps
```

Kein Secret nötig — das Repo ist **public** auf GitHub. Kein pip-Consent,
keine Python-Deps, non-interactive verifiziert (Exit 0, auf 3 Profilen).

## Warum dieses Plugin

`env-setup` ist der Credential-Kanal für Kundentenants: der Agent fordert
Zugänge **nach Name** im Chat an, der Kunde füllt ein maskiertes
Popup-Formular aus, die Werte werden **direkt in die `.env` des Pods**
geschrieben. Passwörter/Tokens erscheinen nie im Chat, nie in Transkripten,
nie im Modellkontext. Das ist bei MSP-Kunden der Unterschied zwischen
„Hermes einrichten können" und „niemand traut sich, Hermes Zugänge zu geben".

- Repo: <https://github.com/delta-one-net/hermes-env-setup> (public, MIT, v0.3.0)
- Läuft stabil auf 3 Profilen auf dem Hephaestus-Host (default, daedalus, overpoch)
- Doctor-Verifikation: „runtime discovery, manifest parsing, import, and
  registration passed — 2 tool(s)"

## Was im Chart zu bauen ist

### 1. Values (Chart-defaults, abwärtskompatibel)

```yaml
bootstrapPlugins:
  enabled: false          # default false → bestehende Releases unverändert
  plugins: []             # Liste von {repo, ref?}
```

### 2. initContainer-Template (Deployment), values-gated

Der initContainer geht über **`bootstrapPlugins.plugins`** — nichts ist
gehardcoded. Jeder Listeneintrag `{repo, ref?}` wird installiert:

```yaml
{{- if .Values.bootstrapPlugins.enabled }}
initContainers:
  - name: bootstrap-plugins
    image: {{ .Values.image }}        # identischer Ref wie der Main-Container
    {{- if .Values.securityContext }} # gleiche UID/GID wie der Volume-Besitzer
    securityContext: {{- toYaml .Values.securityContext | nindent 12 }}
    {{- end }}
    command: ["sh", "-c"]
    args:
      - |
        set -eu
        {{- range $p := .Values.bootstrapPlugins.plugins }}
        if hermes plugins install {{ $p.repo }} --enable --yes-deps {{ if $p.ref }}--ref {{ $p.ref }} {{ end }}; then
          echo "bootstrap: {{ $p.repo }} ok"
        elif hermes plugins update {{ $p.repo }}; then
          echo "bootstrap: {{ $p.repo }} updated (was already present)"
        else
          echo "WARN: bootstrap failed for {{ $p.repo }} (pod starts anyway)"
        fi
        {{- end }}
    volumeMounts:
      # Init containers nutzen dieselben volumes wie normale Container:
      # die Pod-volumes-Sektion (claim-gebunden) bleibt, wie sie ist — der
      # initContainer mountet nur denselben Namen + mountPath, den der
      # Main-Container für den persistenten Data-Pfad verwendet. Kein
      # zusätzliches Volume, keine zusätzliche Spec.
      - name: {{ <volume name aus dem Main-Container-Template> }}
        mountPath: {{ <mountPath aus dem Main-Container-Template> }}
{{- end }}
```

**Kritische Punkte:**

- **Idempotenz:** `plugins install` schlägt mit „already exists" fehl, wenn
  das Plugin schon im Volume liegt → per `elif plugins update` abfangen. Der
  initContainer darf **nie** einen Crash-Loop produzieren, nur weil ein
  Plugin bereits installiert ist. Deshalb der `else`-Zweig als
  non-fatal-Exit.
- **`--ref` pinnt auf einen immutablen 40-Zeichen-Git-SHA** (CLI-Vertrag:
  `hermes plugins install --ref COMMIT_SHA`). Kurz-SHAs lehnt die CLI ab —
  beim Pinning also den vollen SHA eintragen (siehe Abschnitt 4).
- **Dasselbe Volume wie der Main-Container**, sonst installiert der
  initContainer ins Leere und der Pod startet ohne Plugin. Gleiche UID/GID
  wie der Volume-Besitzer (im Docker-Referenzlayout der Profile-Files: 10000).
- **`--enable`** schreibt gleichzeitig den `plugins.enabled`-Eintrag in die
  Profil-Config — kein zweiter Konfigurationsschritt nötig.
- **Egress:** der initContainer braucht HTTPS zu `github.com` (und
  `codeload.github.com` beim Clone). Falls die Cluster-Policy das
  einschränkt, bräuchte es eine NetworkPolicy-Ausnahme oder wir pinnen
  später auf ein internes Mirror-Repo (GitLab-Proxy).

### 3. Rollout

- **Bestehende Tenants:** in jeder HelmRelease unter
  `clusters/prod/apps/hermes/tenants/<tenant>.yaml` den `values:`-Block
  ergänzen — **mit der plugins-Liste**, denn `enabled: true` allein ist eine
  No-op (der initContainer iteriert nur, was in `plugins` steht):

  ```yaml
  values:
    bootstrapPlugins:
      enabled: true
      plugins:
        - repo: delta-one-net/hermes-env-setup
  ```

  → Flux reconciles → Pod-Rollout → init läuft → Plugin + Tools + Skill sind
  in jeder neuen Session des Tenants. Kein manueller Schritt danach.

- **Neue Tenants:** die drei Zeilen ins Runbook-Kapitel „GitOps / Flux"
  aufnehmen — fertig. Ab dann ist Plugin-Versorgung implizit.

### 4. Empfehlung: Pinning für Kundentenants

Für interne/Entwicklungs-Tenants: `main` ziehen (aktueller Stand beim
Pod-Start). Für Kundentenants empfehle ich `ref:`-Pinning auf einen
getesteten **40-Zeichen-Commit-SHA** (CLI-Vertrag, Kurz-SHAs werden
abgelehnt) — dann rollt nur, was abgesegnet wurde:

```yaml
bootstrapPlugins:
  enabled: true
  plugins:
    - repo: delta-one-net/hermes-env-setup
      ref: 54ac821356883e2de3cb65834db3c835b8a57ec1   # v0.3.5, bewusst bumpen
```

## Offene Punkte, die nur du beantworten kannst (2 Minuten)

1. **Volume-Name + Mount-Pfad:** beantwortet durch dein Review — init
   containers nutzen dieselben volume-Definitionen wie normale Container.
   Im Template einfach den identischen Namen + mountPath aus deinem
   Main-Container-Template für den persistenten Data-Mount wiederverwenden
   (ggf. als Helm-Partial), die claim-gebundene Pod-volumes-Sektion bleibt
   unverändert.
2. UID/GID des Volume-Besitzers (für den initContainer-SecurityContext)
3. Egress-Policy: HTTPS zu github.com erlaubt?
4. Sollen die `bootstrapPlugins.plugins` direkt pro Tenant-Werte überschreibbar
   sein (Liste per Helm-Value, wie oben im Rollout gezeigt) oder reicht ein
   Chart-Default? Anmerkung: der offene Fleet-MR !1 (`do-nick`) setzt bislang
   nur `enabled: true` — er bräuchte die plugins-Liste ergänzt, sobald der
   Chart das Feature hat.

## Abgeprüfte Alternativen (nicht bauen)

- **ConfigMap/Volume-Mount nach `plugins/`:** read-only-Mount kollidiert mit
  dem Installer-Update-Pfad; jedes Plugin-Update würde eine
  Flux-Reconcile-Choreografie. Unnötig, solange das Plugin per git clone
  installierbar ist.
- **Custom-Image mit eingebackenem Plugin:** der spätere Ausbau — wenn die
  Delta-One-Distribution steht, wird der initContainer-Payload einfach
  `hermes profile install <distribution>` statt des nackten Plugins. Der Hook
  bleibt identisch, wir tauschen nur den Payload. Nicht jetzt.

---

*Verifiziert am Hephaestus-Host (Hermes v0.22.4): Installation + Enable +
Doctor auf 3 Profilen grün; non-interactive (`--enable --yes-deps`) Exit 0;
Registry-Konflikt-Fall (`already exists`) per `plugins update` abgefangen.*