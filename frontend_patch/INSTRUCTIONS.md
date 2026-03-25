# Frontend Patch Instructions

These patches add a **Morning Briefing** tab to the MimiWidget and a briefing
endpoint to the admin API route.

**Do NOT apply these automatically.** Review each patch and apply manually
after reading the morning briefing.

---

## Files to modify

| File | Change | Risk |
|------|--------|------|
| `src/app/api/admin/mimi/route.ts` | Add `GET ?view=briefing` endpoint | Low |
| `src/components/admin/MimiWidget.tsx` | Add "Morning Briefing" tab | Low |

Both changes are purely additive — no existing logic is modified.

---

## Prerequisites

- `~/Mimi/briefing.py` must have run at least once (creates `reviews/` directory)
- `ADMIN_STATUS_SECRET` must be set in `.env.local`
- Frontend must be running on port 3000

---

## How to apply

```bash
cd ~/Desktop/"Project Succession"/frontend

# 1. Review the patches below
# 2. Apply manually using your editor — do NOT use `git apply` on these files,
#    they are diffs relative to the current state, not git-format patches

# 3. After applying, run:
npm run lint       # must be 0 errors 0 warnings
npm run build      # must succeed
```

---

## Testing after apply

1. Open `/admin/status` in the browser
2. Expand the MimiWidget
3. Click the "Briefing" tab
4. Verify the morning briefing markdown renders correctly
5. If no briefing exists yet, you should see: "No briefing available — run overnight.py first"

---

## Rollback

Both changes are in separate sections of their respective files.
To rollback: remove the added code blocks and revert to the state
before applying the patch.
