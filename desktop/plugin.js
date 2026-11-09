/**
 * env-setup — Setup Assistant desktop half.
 *
 * The agent requests credentials by NAME when the chat needs them (on
 * demand); this half makes that request impossible to miss:
 *
 *   - a new request pops up as a MODAL dialog automatically (hosted by the
 *     always-mounted statusbar chip; Radix portal, so it floats over the app)
 *   - Esc / backdrop / close = "later" (request stays open, chip keeps pulsing)
 *   - the Cancel button really ends the ask flow server-side; the waiting
 *     agent tool returns 'cancelled'
 *   - the /setup page mirrors the same form inline for anyone who prefers it
 *
 * Security invariant: values are POSTed to the plugin backend and written to
 * .env there. They never appear in the chat, in tool results, or in any
 * query cache that the agent can read.
 *
 * Plain ESM, loaded uncompiled — jsx() calls only, no JSX syntax.
 */

import {
  cn, haptic, host, usePluginI18n, useQuery, useQueryClient, queryClient,
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
  ROUTES_AREA, useValue,
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'env-setup'

let _ctx = null

// Request ids that have already auto-popped once: a dismissed dialog must not
// nag again for the same request. New requests (new id) pop again. Chip clicks
// always reopen. App restart resets — acceptable, the form is still findable.
const _seenAutoOpen = new Set()

const api = {
  // ?profile= scopes the view to this profile's requests so a profile's
  // desktop never sees (or resolves) another profile's credential form.
  requests: (profile) => _ctx.rest('/requests' + (profile ? `?profile=${encodeURIComponent(profile)}` : '')),
  resolve: (rid, values, skipped) =>
    _ctx.rest(`/requests/${rid}/resolve`, { method: 'POST', body: { values, skipped } }),
  cancel: (rid) => _ctx.rest(`/requests/${rid}/cancel`, { method: 'POST', body: {} }),
}

// ── shared bits ─────────────────────────────────────────────────────────────

const pill = cn(
  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[0.6875rem] font-medium',
  'border border-(--ui-stroke-secondary) text-(--ui-text-tertiary)'
)

const inputCls = cn(
  'w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent',
  'px-2.5 py-1.5 text-sm outline-none',
  'focus:border-(--ui-accent) placeholder:text-(--ui-text-quaternary)'
)

const btnCls = cn(
  'inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium',
  'bg-(--ui-accent) text-white transition-opacity hover:opacity-90 disabled:opacity-50'
)

const btnGhostCls = cn(
  'inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm',
  'border border-(--ui-stroke-secondary) text-(--ui-text-secondary)',
  'transition-colors hover:bg-(--chrome-action-hover)'
)

function FieldRow({ field, value, onChange }) {
  const t = usePluginI18n(ID)
  return jsxs('label', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', { className: 'text-sm font-medium', children: field.label }),
          field.required
            ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: t('required') })
            : null,
          field.secret
            ? jsx('span', { className: pill, children: '🔒 ' + t('masked') })
            : null,
          field.set
            ? jsx('span', { className: pill, children: '✓ ' + t('alreadySet') })
            : null,
        ],
      }),
      jsx('input', {
        className: inputCls,
        type: field.secret ? 'password' : 'text',
        value: value ?? '',
        placeholder: field.example || field.name,
        autoComplete: 'off',
        autoCorrect: 'off',
        spellCheck: false,
        onChange: (e) => onChange(field.name, e.target.value),
      }),
      field.description
        ? jsx('span', { className: 'text-[0.75rem] text-(--ui-text-quaternary)', children: field.description })
        : null,
      field.url
        ? jsx('a', {
            href: field.url,
            className: 'text-[0.75rem] text-(--ui-accent) hover:underline',
            onClick: (e) => {
              e.preventDefault()
              if (_ctx && _ctx.os) _ctx.os.openExternal(field.url)
            },
            children: t('whereToGet'),
          })
        : null,
    ],
  })
}

function ResultPanel({ result, onDone, t }) {
  const lines = []
  if (result.configured && result.configured.length) {
    lines.push(
      jsxs('div', {
        key: 'ok',
        className: 'flex items-start gap-2',
        children: [
          jsx('span', { children: '✅' }),
          jsxs('span', {
            children: [t('saved'), ' ', jsx('code', { children: result.configured.join(', ') })],
          }),
        ],
      })
    )
  }
  if (result.skipped && result.skipped.length) {
    lines.push(
      jsxs('div', {
        key: 'sk',
        className: 'flex items-start gap-2',
        children: [
          jsx('span', { children: '↩️' }),
          jsxs('span', {
            children: [t('skipped'), ' ', jsx('code', { children: result.skipped.join(', ') })],
          }),
        ],
      })
    )
  }
  return jsxs('div', {
    className: 'flex flex-col gap-4',
    children: [
      jsx('div', {
        className: cn('rounded-lg border p-4',
          result.ok ? 'border-(--ui-stroke-secondary)' : 'border-red-400'),
        children: lines,
      }),
      result.errors && result.errors.length
        ? jsx('div', {
            className: 'text-sm text-red-400',
            children: result.errors.join(' · '),
          })
        : null,
      jsx('div', { className: 'text-[0.75rem] text-(--ui-text-quaternary)', children: t('privacyNote') }),
      jsx('button', { className: btnGhostCls, onClick: onDone, type: 'button', children: t('done') }),
    ],
  })
}

// ── the ask form (shared by the auto-popup dialog and the /setup page) ──────

function SetupForm({ request, onResolve, onCancel, onResolved, onDone, t }) {
  const fields = request.fields
  const [values, setValues] = useState({})
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [missingRequired, setMissingRequired] = useState([])

  useEffect(() => {
    setResult(null)
    setValues({})
    setMissingRequired([])
  }, [request.id])

  if (result) return jsx(ResultPanel, { result, onDone, t })

  const change = (name, v) => setValues((prev) => ({ ...prev, [name]: v }))

  const submit = async () => {
    const missing = fields.filter((f) => f.required && !(values[f.name] || '').trim()).map((f) => f.name)
    if (missing.length) {
      setMissingRequired(missing)
      return
    }
    setMissingRequired([])
    const provided = {}
    const skipped = []
    for (const f of fields) {
      const v = (values[f.name] || '').trim()
      if (v) provided[f.name] = v
      else if (!f.set) skipped.push(f.name) // empty+unset = user chose to skip
    }
    setBusy(true)
    try {
      const res = await onResolve(provided, skipped)
      setResult({ ok: !res.errors, ...res })
      if (onResolved) onResolved()
    } catch (err) {
      setResult({ ok: false, errors: [String((err && err.message) || err)] })
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-4',
    children: [
      jsx('div', { className: 'text-[0.75rem] text-(--ui-text-quaternary)', children: t('privacyNote') }),
      jsx('div', {
        className: 'flex max-h-[50vh] flex-col gap-4 overflow-y-auto',
        children: fields.map((f) =>
          jsx('div', {
            className: missingRequired.includes(f.name) ? 'rounded-md border border-red-400 p-2' : '',
            children: jsx(FieldRow, { field: f, value: values[f.name], onChange: change }),
          }, f.name)
        ),
      }),
      missingRequired.length
        ? jsx('div', { className: 'text-sm text-red-400', children: t('missingRequired') })
        : null,
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx('button', {
          type: 'button',
          className: btnCls,
          disabled: busy,
          onClick: () => { haptic('tap'); submit() },
          children: busy ? t('saving') : t('save'),
        }),
        jsx('button', {
          type: 'button',
          className: btnGhostCls,
          disabled: busy,
          onClick: () => { haptic('tap'); onCancel() },
          children: t('cancel'),
        }),
      ] }),
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('dismissHint') }),
    ],
  })
}

// ── page (/setup) — mirror of the dialog for anyone who prefers the sidebar ─

function SetupPage() {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const profile = useValue(host.state.focusedSessionProfile)
  const requestsQuery = useQuery({
    queryKey: [ID, 'requests', profile],
    queryFn: () => api.requests(profile),
    refetchInterval: 5000,
  })

  const [active, setActive] = useState(null)
  const open = ((requestsQuery.data || {}).requests || []).filter((r) => r.status === 'open')
  const pending = open[0] || null

  useEffect(() => { if (pending) setActive(pending) }, [pending])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: [ID] })

  if (active) {
    return jsxs('div', {
      className: 'mx-auto flex max-w-xl flex-col gap-4 p-6',
      children: [
        jsxs('div', {
          className: 'flex flex-col gap-1 rounded-lg border border-(--ui-accent) p-4',
          children: [
            jsxs('span', { className: 'text-sm font-semibold', children: ['🔐 ', t('agentAsks')] }),
            jsx('h2', { className: 'text-base font-semibold', children: active.title }),
            active.reason
              ? jsx('span', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: active.reason })
              : null,
          ],
        }),
        jsx(SetupForm, {
          request: active,
          t,
          onResolve: async (values, skipped) => {
            const res = await api.resolve(active.id, values, skipped)
            invalidate()
            return res
          },
          onCancel: async () => {
            await api.cancel(active.id)
            invalidate()
            setActive(null)
          },
          onDone: () => setActive(null),
        }),
      ],
    })
  }

  return jsxs('div', {
    className: 'mx-auto flex max-w-xl flex-col gap-4 p-6',
    children: [
      jsxs('div', { className: 'flex flex-col gap-2', children: [
        jsx('h1', { className: 'text-lg font-semibold', children: t('pageTitle') }),
        jsx('span', { className: 'text-sm text-(--ui-text-tertiary)', children: t('pageSubtitle') }),
      ]}),
      jsxs('div', {
        className: 'flex flex-col gap-1 rounded-lg border border-(--ui-stroke-secondary) p-4',
        children: [
          jsxs('span', { className: 'text-sm font-medium', children: ['🔐 ', t('idleTitle')] }),
          jsx('span', { className: 'text-[0.75rem] text-(--ui-text-quaternary)', children: t('idleHint') }),
        ],
      }),
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('footer') }),
    ],
  })
}

// ── statusbar chip + auto-popup dialog host ────────────────────────────────

function SetupChip() {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const profile = useValue(host.state.focusedSessionProfile)
  const requestsQuery = useQuery({
    queryKey: [ID, 'requests', profile],
    queryFn: () => api.requests(profile),
    refetchInterval: 5000,
  })

  const [active, setActive] = useState(null)       // request shown in the dialog
  const [dialogOpen, setDialogOpen] = useState(false)
  const [keepOpenForResult, setKeepOpenForResult] = useState(false)

  const requests = (requestsQuery.data || {}).requests || []
  const open = requests.filter((r) => r.status === 'open')
  const pending = open[0] || null

  const invalidate = () => queryClient.invalidateQueries({ queryKey: [ID] })

  // Auto-popup: a NEW open request pops the dialog up, exactly once per id.
  useEffect(() => {
    if (!pending) return
    setActive(pending)
    if (!_seenAutoOpen.has(pending.id)) {
      _seenAutoOpen.add(pending.id)
      setDialogOpen(true)
    }
  }, [pending && pending.id])

  // Auto-close when the active request was closed from elsewhere (agent
  // superseded it, or the page resolved it) — unless THIS dialog just
  // submitted and is showing its result panel.
  useEffect(() => {
    if (!active || keepOpenForResult) return
    const now = requests.find((r) => r.id === active.id)
    if (!now || now.status !== 'open') {
      setDialogOpen(false)
      setActive(null)
    }
  }, [requests, active, keepOpenForResult])

  const closeDialog = () => {
    setDialogOpen(false)
    setKeepOpenForResult(false)
    // Esc/backdrop = "later": request stays open, chip keeps pulsing.
  }

  const cancelFlow = async () => {
    if (active) await api.cancel(active.id)
    invalidate()
    setDialogOpen(false)
    setActive(null)
    setKeepOpenForResult(false)
  }

  const resolveFlow = async (values, skipped) => {
    const res = await api.resolve(active.id, values, skipped)
    invalidate()
    setKeepOpenForResult(true)
    return res
  }

  return jsxs('div', {
    className: 'contents',
    children: [
      jsx('button', {
        type: 'button',
        title: pending ? t('chipActiveTip') : t('chipTip'),
        className: cn(
          'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] font-medium transition-colors',
          pending
            ? 'text-(--ui-accent) bg-(--chrome-action-hover) animate-pulse'
            : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
        ),
        onClick: () => {
          haptic('tap')
          if (pending) {
            setActive(pending)
            setDialogOpen(true)
          } else {
            host.navigate('/setup')
          }
        },
        children: open.length ? `🔐 ${open.length}` : '🔐',
      }),
      jsx(Dialog, {
        open: dialogOpen && !!active,
        onOpenChange: (o) => { if (!o) closeDialog() },
        children: active
          ? jsx(DialogContent, {
              className: 'sm:max-w-lg',
              children: jsxs('div', {
                className: 'flex flex-col gap-4',
                children: [
                  jsxs(DialogHeader, {
                    children: [
                      jsx(DialogTitle, { children: `🔐 ${active.title}` }),
                      jsx(DialogDescription, {
                        children: active.reason
                          ? `${t('agentAsks')} — ${active.reason}`
                          : t('agentAsks'),
                      }),
                    ],
                  }),
                  jsx(SetupForm, {
                    request: active,
                    t,
                    onResolve: resolveFlow,
                    onCancel: cancelFlow,
                    onDone: closeDialog,
                  }),
                ],
              }),
            })
          : null,
      }),
    ],
  })
}

// ── registration ───────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Setup Assistant',
  register(ctx) {
    _ctx = ctx

    ctx.i18n.register({
      de: {
        pageTitle: 'Zugänge',
        pageSubtitle: 'Hermes fragt Zugänge direkt im Chat an, sobald sie gebraucht werden — das Formular poppt dann automatisch auf.',
        idleTitle: 'Keine offene Anfrage',
        idleHint: 'Wenn im Chat Zugänge benötigt werden (Mail, Tokens, Passwörter), öffnet sich das Formular automatisch. Werte landen direkt in der .env — nie im Chat.',
        agentAsks: 'Hermes bittet um Zugänge',
        save: 'In .env speichern',
        saving: 'Speichert…',
        saved: 'Gespeichert:',
        skipped: 'Leer gelassen:',
        cancel: 'Abbrechen',
        done: 'Fertig',
        required: 'Pflichtfeld',
        masked: 'maskiert',
        alreadySet: 'bereits gesetzt',
        whereToGet: 'Wo bekomm ich das? →',
        missingRequired: 'Bitte die markierten Pflichtfelder ausfüllen.',
        privacyNote: 'Deine Werte werden direkt in die .env geschrieben — sie erscheinen nie im Chat und nie im Modellkontext.',
        dismissHint: 'Esc oder Klick daneben = später. Abbrechen beendet die Anfrage.',
        chipTip: 'Zugänge',
        chipActiveTip: 'Hermes wartet auf Zugänge — klicken zum Ausfüllen',
        footer: 'Integrationen deklarieren ihre Zugänge im plugin.yaml (requires_env) — das Plugin übernimmt sie automatisch.',
      },
      en: {
        pageTitle: 'Credentials',
        pageSubtitle: 'Hermes asks for credentials in the chat the moment they are needed — the form pops up automatically.',
        idleTitle: 'No open request',
        idleHint: 'When the chat needs credentials (mail, tokens, passwords), the form opens automatically. Values go straight to .env — never into the chat.',
        agentAsks: 'Hermes is asking for credentials',
        save: 'Save to .env',
        saving: 'Saving…',
        saved: 'Saved:',
        skipped: 'Left empty:',
        cancel: 'Cancel',
        done: 'Done',
        required: 'required',
        masked: 'masked',
        alreadySet: 'already set',
        whereToGet: 'Where do I get this? →',
        missingRequired: 'Please fill the marked required fields.',
        privacyNote: 'Your values are written straight into .env — they never appear in the chat or the model context.',
        dismissHint: 'Esc or click outside = later. Cancel ends the request.',
        chipTip: 'Credentials',
        chipActiveTip: 'Hermes is waiting for credentials — click to fill the form',
        footer: 'Integrations declare their credentials in plugin.yaml (requires_env) — the plugin picks them up automatically.',
      },
    })

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/setup' },
      render: () => jsx(SetupPage, {}),
    })

    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 140,
      render: () => jsx(SetupChip, {}),
    })

    host.onEvent('plugin.env-setup.request.created', ({ payload }) => {
      const r = payload && payload.request
      host.notify({
        kind: 'info',
        message: r && r.title ? `🔐 ${r.title}` : '🔐 Zugänge werden angefragt',
      })
      try {
        ctx.os.notify({
          title: 'Hermes braucht Zugänge',
          body: (r && r.reason) || 'Ein Formular ist gerade aufgepoppt.',
          activate: '/setup',
        })
      } catch (e) { /* notifications may be disabled — not fatal */ }
    })

    for (const evt of ['request.resolved', 'request.cancelled']) {
      host.onEvent(`plugin.env-setup.${evt}`, () => {
        try {
          queryClient.invalidateQueries({ queryKey: [ID] })
        } catch (e) { /* poll covers it */ }
      })
    }
  },
}