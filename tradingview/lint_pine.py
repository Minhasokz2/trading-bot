"""Offline checks for the TradingView indicator (there is no Pine compiler outside TradingView).

    python tradingview/lint_pine.py tradingview/coin_audit_library.pine          # full (grammar parse is slow: minutes)
    python tradingview/lint_pine.py --fast tradingview/coin_audit_library.pine   # names, arguments, layout (seconds)

1. Syntax: parsed with `pynescript` (an ANTLR grammar of Pine Script) when it is installed.
2. Names: every namespaced function / constant (`ta.ema`, `label.style_label_up`, ...) must be a real Pine v6
   built-in, every named argument must exist on that function, every bare call must be a built-in or a function
   defined in the script.
3. Layout rules TradingView enforces that the grammar does not: wrapped lines must NOT be indented by a multiple
   of 4 spaces, no tabs, no `:=` before a declaration, no bare `na` inside `array.from`, no duplicate globals.
4. Pine v6 evaluates `and` / `or` lazily: a stateful call (ta.*, math.sum, request.*, or a user function that keeps
   history) after `and`, `or` or `?` does not run on every candle — an error here. The same call inside a local block
   (if / for) is a warning, as are locals shadowing globals.
Exit code 1 if there is any error. The allowlist covers the v6 reference for everything this project uses and the
common rest; extend it when you use a new built-in (with its exact name from the Pine v6 reference manual).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NS_FUNCS = {
    "ta": "alma atr barssince bb bbw cci change cmo cog correlation cross crossover crossunder cum dev dmi ema falling "
          "highest highestbars hma kc kcw linreg lowest lowestbars macd max median mfi min mode mom percentile_linear_interpolation "
          "percentile_nearest_rank percentrank pivothigh pivotlow range rising rma roc rsi sar sma stdev stoch supertrend swma "
          "tr tsi valuewhen variance vwap vwma wma wpr pivot_point_levels",
    "math": "abs acos asin atan avg ceil cos exp floor log log10 max min pow random round round_to_mintick sign sin sqrt "
            "sum tan todegrees toradians",
    "array": "abs avg binary_search binary_search_leftmost binary_search_rightmost clear concat copy covariance every "
             "fill first from get includes indexof insert join last lastindexof max median min mode new new_bool "
             "new_box new_color new_float new_int new_label new_line new_linefill new_string new_table percentile_linear_interpolation "
             "percentile_nearest_rank percentrank pop push range remove reverse set shift size slice some sort sort_indices "
             "standardize stdev sum unshift variance",
    "str": "contains endswith format format_time length lower match pos repeat replace replace_all split startswith "
           "substring tonumber tostring trim upper",
    "table": "cell cell_set_bgcolor cell_set_height cell_set_text cell_set_text_color cell_set_text_font_family "
             "cell_set_text_formatting cell_set_text_halign cell_set_text_size cell_set_text_valign cell_set_tooltip "
             "cell_set_width clear delete merge_cells new set_bgcolor set_border_color set_border_width set_frame_color "
             "set_frame_width set_position",
    "label": "copy delete get_text get_x get_y new set_color set_point set_size set_style set_text set_text_font_family "
             "set_text_formatting set_textalign set_textcolor set_tooltip set_x set_xloc set_xy set_y set_yloc",
    "line": "copy delete get_price get_x1 get_x2 get_y1 get_y2 new set_color set_extend set_first_point set_second_point "
            "set_style set_width set_x1 set_x2 set_xloc set_xy1 set_xy2 set_y1 set_y2",
    "box": "copy delete get_bottom get_left get_right get_top new set_bgcolor set_border_color set_border_style "
           "set_border_width set_bottom set_bottom_right_point set_extend set_left set_lefttop set_right set_rightbottom "
           "set_text set_text_color set_text_font_family set_text_formatting set_text_halign set_text_size set_text_valign "
           "set_text_wrap set_top set_top_left_point set_xloc",
    "linefill": "delete get_line1 get_line2 new set_color",
    "color": "b from_gradient g new r rgb t",
    "request": "currency_rate dividends earnings economic financial quandl security security_lower_tf seed splits",
    "timeframe": "change from_seconds in_seconds",
    "input": "bool color enum float int price session source string symbol text_area time timeframe",
    "runtime": "error",
    "log": "error info warning",
    "map": "clear contains copy get keys new put put_all remove size values",
    "chart.point": "copy from_index from_time new now",
    "polyline": "delete new",
    "ticker": "heikinashi inherit kagi linebreak modify new pointfigure renko standard",
}
NS_CONSTS = {
    "color": "aqua black blue fuchsia gray green lime maroon navy olive orange purple red silver teal white yellow",
    "size": "auto huge large normal small tiny",
    "position": "bottom_center bottom_left bottom_right middle_center middle_left middle_right top_center top_left top_right",
    "shape": "arrowdown arrowup circle cross diamond flag labeldown labelup square triangledown triangleup xcross",
    "location": "abovebar absolute belowbar bottom top",
    "label": "all style_arrowdown style_arrowup style_circle style_cross style_diamond style_flag style_label_center "
             "style_label_down style_label_left style_label_lower_left style_label_lower_right style_label_right "
             "style_label_up style_label_upper_left style_label_upper_right style_none style_square style_text_outline "
             "style_triangledown style_triangleup style_xcross",
    "line": "all style_arrow_both style_arrow_left style_arrow_right style_dashed style_dotted style_solid",
    "box": "all", "table": "all", "linefill": "all", "polyline": "all",
    "extend": "both left none right",
    "text": "align_bottom align_center align_left align_right align_top format_bold format_italic format_none wrap_auto wrap_none",
    "font": "family_default family_monospace",
    "plot": "style_area style_areabr style_circles style_columns style_cross style_histogram style_line style_linebr "
            "style_stepline style_stepline_diamond style_steplinebr linestyle_dashed linestyle_dotted linestyle_solid",
    "display": "all data_window none pane price_scale status_line",
    "alert": "freq_all freq_once_per_bar freq_once_per_bar_close",
    "barmerge": "gaps_off gaps_on lookahead_off lookahead_on",
    "xloc": "bar_index bar_time", "yloc": "abovebar belowbar price",
    "format": "inherit mintick percent price volume",
    "barstate": "isconfirmed isfirst ishistory islast islastconfirmedhistory isnew isrealtime",
    "syminfo": "basecurrency country currency description employees industry mincontract minmove mintick pointvalue "
               "prefix pricescale root sector shareholders ticker tickerid timezone type volumetype main_tickerid",
    "timeframe": "isdaily isdwm isintraday isminutes ismonthly isseconds isticks isweekly main_period multiplier period",
    "chart": "bg_color fg_color is_heikinashi is_kagi is_linebreak is_pnf is_range is_renko is_standard left_visible_bar_time right_visible_bar_time",
    "ta": "accdist iii nvi obv pvi pvt tr vwap wad wvad",
    "math": "e phi pi rphi",
    "hline": "style_dashed style_dotted style_solid",
    "order": "ascending descending",
    "dividends": "gross net", "earnings": "actual estimate standardized", "splits": "denominator numerator",
    "session": "extended regular ismarket ispremarket ispostmarket isfirstbar islastbar isfirstbar_regular islastbar_regular",
    "currency": "USD EUR BTC ETH USDT NONE",
    "scale": "left none right", "adjustment": "dividends none splits",
}
BARE_FUNCS = set("""alert alertcondition barcolor bgcolor bool box color dayofmonth dayofweek fill fixnan float hline
    hour indicator int label library line max_bars_back minute month na nz plot plotarrow plotbar plotcandle plotchar
    plotshape second strategy string table time time_close timestamp weekofyear year linefill""".split())
BUILTIN_VARS = set("""open high low close volume hl2 hlc3 ohlc4 hlcc4 bar_index last_bar_index last_bar_time time time_close
    time_tradingday timenow na true false""".split())
PARAMS = {
    "indicator": "title shorttitle overlay format precision scale max_bars_back timeframe timeframe_gaps explicit_plot_zorder "
                 "max_lines_count max_labels_count max_boxes_count calc_bars_count max_polylines_count dynamic_requests behind_chart",
    "input.string": "defval title options tooltip inline group confirm display active",
    "input.int": "defval title minval maxval step options tooltip inline group confirm display active",
    "input.float": "defval title minval maxval step options tooltip inline group confirm display active",
    "input.bool": "defval title tooltip inline group confirm display active",
    "input.color": "defval title tooltip inline group confirm display active",
    "input.symbol": "defval title tooltip inline group confirm display active",
    "input.timeframe": "defval title options tooltip inline group confirm display active",
    "plot": "series title color linewidth style trackprice histbase offset join editable show_last display format precision force_overlay linestyle",
    "plotshape": "series title style location color offset text textcolor editable size show_last display format precision force_overlay",
    "plotchar": "series title char location color offset text textcolor editable size show_last display format precision force_overlay",
    "bgcolor": "color offset editable show_last title display force_overlay",
    "label.new": "x y text xloc yloc color style textcolor size textalign tooltip text_font_family force_overlay text_formatting point",
    "line.new": "x1 y1 x2 y2 xloc extend color style width force_overlay first_point second_point",
    "box.new": "left top right bottom border_color border_width border_style extend xloc bgcolor text text_size text_color "
               "text_halign text_valign text_wrap text_font_family force_overlay text_formatting top_left bottom_right",
    "table.new": "position columns rows bgcolor frame_color frame_width border_color border_width force_overlay",
    "table.cell": "table_id column row text width height text_color text_halign text_valign text_size bgcolor tooltip "
                  "text_font_family text_formatting",
    "request.security": "symbol timeframe expression gaps lookahead ignore_invalid_symbol currency calc_bars_count",
    "alert": "message freq", "alertcondition": "condition title message",
    "str.tostring": "value format", "color.new": "color transp",
    "color.from_gradient": "value bottom_value top_value bottom_color top_color",
    "linefill.new": "line1 line2 color",
}


def _names(s: str) -> set:
    return set(s.split())


FUNCS = {ns: _names(v) for ns, v in NS_FUNCS.items()}
CONSTS = {ns: _names(v) for ns, v in NS_CONSTS.items()}
PARAMS_ = {k: _names(v) for k, v in PARAMS.items()}


def strip_comment(line: str) -> str:
    out, q = [], ""
    i = 0
    while i < len(line):
        ch = line[i]
        if q:
            out.append(ch)
            if ch == "\\":
                out.append(line[i + 1:i + 2]); i += 2; continue
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch; out.append(ch)
        elif line.startswith("//", i):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def layout_checks(src: str) -> tuple[list, list]:
    errors, warnings = [], []
    lines = src.splitlines()
    depth = 0
    cont_ops = (" ?", " :", " +", " -", " *", " /", " and", " or", ",", "(", "[", "=", "=>")
    for n, raw in enumerate(lines, 1):
        if "\t" in raw:
            errors.append(f"{n}: tab character (Pine needs spaces)")
        code = strip_comment(raw).rstrip()
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        prev = ""
        for k in range(n - 2, -1, -1):
            pc = strip_comment(lines[k]).rstrip()
            if pc.strip():
                prev = pc; break
        is_cont = depth > 0 or (prev.endswith(cont_ops) and not prev.rstrip().endswith("=>"))
        if is_cont and indent % 4 == 0 and depth > 0:
            errors.append(f"{n}: wrapped line indented by {indent} spaces — use a number that is NOT a multiple of 4")
        elif not depth and prev.endswith((" ?", " :", " +", " and", " or")) and indent % 4 == 0:
            errors.append(f"{n}: wrapped line indented by {indent} spaces — use a number that is NOT a multiple of 4")
        q = ""
        for ch in code:
            if q:
                if ch == q: q = ""
                continue
            if ch in "\"'": q = ch; continue
            if ch in "([": depth += 1
            elif ch in ")]": depth -= 1
        if depth < 0:
            errors.append(f"{n}: more closing than opening brackets"); depth = 0
        if re.search(r"array\.from\(([^)]*\bna\b)", code) and not re.search(r"array\.from\([^)]*\w+\(na\)", code):
            if re.search(r"(?<![\w.])na(?![\w(])", code.split("array.from", 1)[1]):
                errors.append(f"{n}: bare `na` inside array.from — write float(na) / int(na) so the array type is known")
    if depth != 0:
        errors.append(f"end of file: {depth} unclosed bracket(s)")
    return errors, warnings


def ast_checks(src: str) -> tuple[list, list, dict]:
    from pynescript import ast as A
    errors, warnings, stats = [], [], {"security": 0, "plots": 0}
    try:
        tree = A.parse(src)
    except Exception as e:                                     # syntax error: report and stop
        return [f"syntax: {str(e).splitlines()[0][:200]}"], [], stats
    user_funcs = {node.name for node in A.walk(tree) if isinstance(node, A.FunctionDef)}

    def dotted(node):
        if isinstance(node, A.Name):
            return node.id
        if isinstance(node, A.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, A.Specialize):
            return dotted(node.value)
        return None

    called_attrs = set()
    for node in A.walk(tree):
        if isinstance(node, A.Call):
            name = dotted(node.func)
            if name is None:
                continue
            called_attrs.add(id(node.func))
            if "." in name:
                ns, fn = name.rsplit(".", 1)
                if ns in FUNCS:
                    if fn not in FUNCS[ns]:
                        errors.append(f"unknown function {name}()")
                elif ns not in user_funcs:
                    errors.append(f"unknown namespace in {name}()")
            elif name not in BARE_FUNCS and name not in user_funcs and name not in ("array", "matrix", "map"):
                errors.append(f"unknown function {name}()")
            if name == "request.security":
                stats["security"] += 1
            if name in ("plot", "plotshape", "plotchar", "bgcolor", "plotarrow", "plotcandle", "plotbar", "barcolor", "fill", "hline"):
                stats["plots"] += 1
            allowed = PARAMS_.get(name)
            for arg in node.args:
                if allowed is not None and getattr(arg, "name", None) and arg.name not in allowed:
                    errors.append(f"{name}(): unknown argument '{arg.name}'")
    for node in A.walk(tree):
        if isinstance(node, A.Attribute) and id(node) not in called_attrs:
            name = dotted(node)
            if not name or "." not in name:
                continue
            ns, attr = name.rsplit(".", 1)
            if ns in CONSTS and attr not in CONSTS[ns] and not (ns in FUNCS and attr in FUNCS[ns]):
                errors.append(f"unknown constant / variable {name}")
    return errors, warnings, stats


def _statements(src: str) -> list[tuple[int, int, str]]:
    """(first line, indentation, code) per statement. A line indented by a number that is not a multiple of 4 is a
    wrapped continuation of the statement above (that is how Pine itself tells them apart); comments are removed."""
    out: list[tuple[int, int, str]] = []
    for n, raw in enumerate(src.splitlines(), 1):
        code = strip_comment(raw).rstrip()
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        if out and indent % 4 != 0:
            ln, ind, prev = out[-1]
            out[-1] = (ln, ind, prev + " " + code.strip())
        else:
            out.append((n, indent, code.strip()))
    return out


STATEFUL = r"ta\.(?!tr\b)\w+|math\.sum|request\.\w+"


def _stateful_funcs(stmts) -> set:
    """User functions that keep history (call a ta.* / math.sum / request.* function, a stateful user function, or
    read the history of one of their own parameters with []). Such calls must run on every candle."""
    funcs: dict[str, tuple[list, str]] = {}
    cur = None
    for _, ind, body in stmts:
        m = re.match(r"^([A-Za-z_]\w*)\(([^)]*)\)\s*=>\s*(.*)$", body) if ind == 0 else None
        if m:
            params = [p.split()[-1] for p in m.group(2).split(",") if p.strip()]
            cur = m.group(1)
            funcs[cur] = (params, m.group(3))
        elif ind == 0:
            cur = None
        elif cur:
            funcs[cur] = (funcs[cur][0], funcs[cur][1] + "\n" + body)
    out: set = set()
    changed = True
    while changed:                                             # a function calling a stateful function is stateful too
        changed = False
        for name, (params, body) in funcs.items():
            if name in out:
                continue
            code = _blank(body)
            if re.search(r"(?<![\w.])(" + STATEFUL + r")\s*\(", code) \
                    or any(re.search(r"(?<![\w.])" + re.escape(p) + r"\s*\[", code) for p in params) \
                    or any(re.search(r"(?<![\w.])" + re.escape(f) + r"\s*\(", code) for f in out):
                out.add(name)
                changed = True
    return out


def scope_checks(src: str) -> tuple[list, list]:
    """Declarations, := targets, shadowing, stateful calls in local scopes or behind a lazy and / or / ?: (text based,
    indentation aware; wrapped lines are joined to their statement first)."""
    errors, warnings = [], []
    decl = re.compile(r"^(?:(?:var|varip)\s+)?(?:(?:float|int|bool|string|color|line|label|box|table|linefill)(?:\[\])?\s+)?([A-Za-z_]\w*)\s*=(?!=)")
    tup = re.compile(r"^\[([^\]]+)\]\s*=(?!=)")
    glob, loc, seen = {}, {}, {}
    stmts = _statements(src)
    user_stateful = _stateful_funcs(stmts)
    call_re = re.compile(r"(?<![\w.])(" + STATEFUL + "".join("|" + re.escape(f) for f in sorted(user_stateful)) + r")\s*\(")
    in_func = False
    for n, indent, body in stmts:
        if indent == 0:
            in_func = bool(re.match(r"^\w+\(.*\)\s*=>", body))
        names = []
        m = decl.match(body)
        if m and not body.startswith(("if ", "for ", "else", "while ", "switch")):
            names.append(m.group(1))
        m2 = tup.match(body)
        if m2:
            names += [x.strip() for x in m2.group(1).split(",")]
        for nm in names:
            if indent == 0:
                if nm in glob:
                    errors.append(f"{n}: '{nm}' declared twice at global scope (first at line {glob[nm]})")
                glob.setdefault(nm, n)
            else:
                loc.setdefault(nm, n)
            seen.setdefault(nm, n)
        m3 = re.match(r"^([A-Za-z_]\w*)\s*:=", body)
        if m3 and m3.group(1) not in seen:
            errors.append(f"{n}: ':=' on '{m3.group(1)}' before it is declared")
        code = _blank(body)
        if indent > 0 and not in_func and call_re.search(code):
            warnings.append(f"{n}: {call_re.search(code).group(1)}() inside a local block — it does not run on every "
                            "candle; call it at global scope and use the result")
        # Pine v6 evaluates `and` / `or` lazily and only the taken branch of `?:` — a stateful call after one of
        # them skips candles and its rolling state goes wrong (e.g. math.sum, ta.sma, x[1] in a user function)
        if not (indent == 0 and re.match(r"^\w+\(.*\)\s*=>", body)):
            lazy = re.search(r"\s(and|or)\s|\?", code)
            if lazy and call_re.search(code[lazy.start():]):
                errors.append(f"{n}: {call_re.search(code[lazy.start():]).group(1)}() after a lazy and / or / ?: — it "
                              "does not run on every candle; compute it into a variable first")
    for nm, n in loc.items():
        if nm in glob and glob[nm] < n:
            warnings.append(f"{n}: local '{nm}' shadows the global declared at line {glob[nm]}")
    return errors, warnings


def _calls(code: str):
    """(name, argument text) for every call in the code (comments and strings blanked), bracket-matched."""
    out = []
    for m in re.finditer(r"(?<![\w.])((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)(?:<\w+>)?\(", code):
        depth, j = 1, m.end()
        while j < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[j], 0)
            j += 1
        out.append((m.group(1), code[m.end():j - 1]))
    return out


def _blank(src: str) -> str:
    """The source with comments removed and string contents replaced by spaces (positions kept)."""
    res = []
    for line in src.splitlines():
        out, quote, esc = [], "", False
        for ch in strip_comment(line):
            if quote:
                if esc:
                    esc = False
                    out.append(" ")
                elif ch == "\\":
                    esc = True
                    out.append(" ")
                elif ch == quote:
                    quote = ""
                    out.append(ch)
                else:
                    out.append(" ")
            else:
                if ch in "\"'":
                    quote = ch
                out.append(ch)
        res.append("".join(out))
    return "\n".join(res)


def fast_name_checks(src: str) -> tuple[list, dict]:
    """Regex version of ast_checks (no grammar): namespaced functions / constants and named arguments."""
    errors, stats = [], {"security": 0, "plots": 0}
    code = _blank(src)
    user_funcs = set(re.findall(r"^([A-Za-z_]\w*)\(.*\)\s*=>", code, re.M))
    called = set()
    for name, args in _calls(code):
        called.add(name)
        if "." in name:
            ns, fn = name.rsplit(".", 1)
            if ns in FUNCS and fn not in FUNCS[ns]:
                errors.append(f"unknown function {name}()")
            elif ns not in FUNCS and ns not in user_funcs and ns not in CONSTS:
                errors.append(f"unknown namespace in {name}()")
        elif name not in BARE_FUNCS and name not in user_funcs and name not in ("if", "for", "while", "switch", "and", "or", "not") \
                and name not in ("array", "matrix", "map"):
            errors.append(f"unknown function {name}()")
        if name == "request.security":
            stats["security"] += 1
        if name in ("plot", "plotshape", "plotchar", "bgcolor", "plotarrow", "plotcandle", "plotbar", "barcolor", "fill", "hline"):
            stats["plots"] += 1
        allowed = PARAMS_.get(name)
        if allowed is not None:
            depth, start, parts = 0, 0, []
            for k, ch in enumerate(args):
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                elif ch == "," and depth == 0:
                    parts.append(args[start:k]); start = k + 1
            parts.append(args[start:])
            for part in parts:
                m = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)", part)
                if m and m.group(1) not in allowed:
                    errors.append(f"{name}(): unknown argument '{m.group(1)}'")
    for m in re.finditer(r"(?<![\w.])([a-z_]+(?:\.[a-z_]+)?)\.([A-Za-z_]\w*)\b(?!\s*[(<])", code):
        ns, attr = m.group(1), m.group(2)
        if ns in CONSTS and attr not in CONSTS[ns] and not (ns in FUNCS and attr in FUNCS[ns]):
            errors.append(f"unknown constant / variable {ns}.{attr}")
    return sorted(set(errors)), stats


def lint(path: str, fast: bool = False) -> int:
    src = Path(path).read_text(encoding="utf-8")
    errors, warnings = layout_checks(src)
    e2, w2 = scope_checks(src)
    errors += e2; warnings += w2
    e4, stats = fast_name_checks(src)
    errors += e4
    if fast:
        parsed = "names checked (fast mode: grammar not parsed)"
    else:
        try:
            e3, w3, _ = ast_checks(src)
            errors += [e for e in e3 if e not in e4]
            warnings += w3
            parsed = "parsed by pynescript" if not any(e.startswith("syntax") for e in e3) else "SYNTAX ERROR"
        except ImportError:
            parsed = "pynescript not installed — syntax not parsed (pip install pynescript)"
    if not src.lstrip().startswith("//@version=6"):
        errors.append("first line must be //@version=6")
    if stats.get("security", 0) > 40:
        errors.append(f"{stats['security']} request.*() calls — TradingView allows 40")
    if stats.get("plots", 0) > 64:
        errors.append(f"{stats['plots']} plot-type calls — TradingView allows 64")
    print(f"{path}: {len(src.splitlines())} lines · {parsed} · request.security {stats.get('security', '?')} · plots {stats.get('plots', '?')}")
    for w in warnings:
        print("  warning:", w)
    for e in errors:
        print("  ERROR:", e)
    print(f"  {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    fast = "--fast" in sys.argv
    files = [a for a in sys.argv[1:] if a != "--fast"] or ["tradingview/coin_audit_library.pine"]
    sys.exit(max(lint(p, fast) for p in files))
