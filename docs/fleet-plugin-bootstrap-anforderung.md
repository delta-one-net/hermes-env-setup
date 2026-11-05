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
mit demselben Image gegen das persistente Data-Volume laufen lässt:

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
        hermes plugins install delta-one-net/hermes-env-setup --enable --yes-deps \
          || hermes plugins update delta-one-net/hermes-env-setup \
          || echo "WARN: env-setup bootstrap failed (pod starts anyway)"
    volumeMounts:
      - name: <DATA_VOLUME_NAME>      # ← dein Name für das persistente Volume
        mountPath: <DATA_MOUNT_PATH>  # ← dein Mount-Pfad (Main-Container-Mount desselben Volumes)
{{- end }}
```

**Kritische Punkte:**

- **Idempotenz:** `plugins install` schlägt mit „already exists" fehl, wenn
  das Plugin schon im Volume liegt → per `|| plugins update` abfangen. Der
  initContainer darf **nie** einen Crash-Loop produzieren, nur weil ein
  Plugin bereits installiert ist. Deshalb der letzte `|| echo` als
  non-fatal-Exit.
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
  ergänzen:

  ```yaml
  values:
    bootstrapPlugins:
      enabled: true
  ```

  → Flux reconciles → Pod-Rollout → init läuft → Plugin + Tools + Skill sind
  in jeder neuen Session des Tenants. Kein manueller Schritt danach.

- **Neue Tenants:** eine Zeile ins Runbook-Kapitel „GitOps / Flux" aufnehmen
  („values: bootstrapPlugins.enabled: true") — fertig. Ab dann ist
  Plugin-Versorgung implizit.

### 4. Empfehlung: Pinning für Kundentenants

Für interne/Entwicklungs-Tenants: `main` ziehen (aktueller Stand beim
Pod-Start). Für Kundentenants empfehle ich `ref:`-Pinning auf einen
getesteten Commit-SHA — dann rollt nur, was abgesegnet wurde:

```yaml
bootstrapPlugins:
  enabled: true
  plugins:
    - repo: delta-one-net/hermes-env-setup
      ref: 1d591a7   # getesteter Stand, bewusst bumpen
```

## Offene Punkte, die nur du beantworten kannst (2 Minuten)

1. Name des persistenten Volumes im Chart + Mount-Pfad im Main-Container
2. UID/GID des Volume-Besitzers (für den initContainer-SecurityContext)
3. Egress-Policy: HTTPS zu github.com erlaubt?
4. Sollen die `bootstrapPlugins.plugins` direkt pro Tenant-Werte überschreibbar
   sein (Liste per Helm-Value) oder reicht ein Chart-Default?

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