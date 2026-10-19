# bot/handlers/admin/design.py
"""Unified admin panel design system.

One visual language for EVERY admin screen:

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
      🛡️ Admin Panel
      Main › Module
      🟢 Status line (optional)
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Rules:
  - ONE fixed header style everywhere (same separator bar width).
  - Breadcrumb shows exactly where the admin is (Main › Module › Action).
  - Back buttons are always "◀ Back" / "🔙 Main Menu".
  - Toggle buttons always use the ON/OFF switch format (sw).
  - These helpers ONLY produce display text; callback_data never changes.
"""

# Fixed separator width used by every panel header.
BAR = "━" * 32


def hd(title: str, crumb: str = "", status: str = "") -> str:
    """Build a consistent panel header (BAR + title + crumb + status + BAR)."""
    lines = [BAR, f"  {title}"]
    if crumb:
        lines.append(f"  {crumb}")
    if status:
        lines.append(f"  {status}")
    lines.append(BAR)
    return "\n".join(lines)


def panel(title: str, crumb: str = "", status: str = "", body: str = "") -> str:
    """Build a full panel message: header + optional body."""
    text = hd(title, crumb, status)
    if body:
        text += "\n\n" + body
    return text


# ------------------------------------------------------------------
#  Switch (ON/OFF) helpers — every boolean control uses these.
#  Stable feature name; only the state changes.
# ------------------------------------------------------------------

def sw(name: str, state: bool) -> str:
    """State-aware switch label: '✅ Name: ON' / '❌ Name: OFF'."""
    return f"{'✅' if state else '❌'} {name}: {'ON' if state else 'OFF'}"


def sw_emoji(name: str, state: bool) -> str:
    """Status-emoji variant used on the dashboards/status buttons:
    '🟢 Name: ON' / '🔴 Name: OFF'."""
    return f"{'🟢' if state else '🔴'} {name}: {'ON' if state else 'OFF'}"


# ------------------------------------------------------------------
#  Compact status line helpers
# ------------------------------------------------------------------

def yn(state: bool) -> str:
    """Compact ON/OFF text with a status emoji."""
    return f"🟢 ON" if state else f"🔴 OFF"
