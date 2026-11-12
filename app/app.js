(function () {
  "use strict";
  var CFG = window.FL_CONFIG || { url: "", anonKey: "" };
  var SPORTS = ["NFL", "NBA", "MLB", "NHL", "MLS", "F1"];
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };

  // ---- session and API ----------------------------------------------------------------------------
  var session = null;
  function loadSession() { try { session = JSON.parse(localStorage.getItem("fl_session") || "null"); } catch (e) { session = null; } }
  function saveSession(s) { session = s; try { if (s) localStorage.setItem("fl_session", JSON.stringify(s)); else localStorage.removeItem("fl_session"); } catch (e) {} }
  function headers(auth) { var h = { "Content-Type": "application/json", apikey: CFG.anonKey }; if (auth) h.Authorization = "Bearer " + auth; return h; }
  function token(grant, body) {
    return fetch(CFG.url + "/auth/v1/token?grant_type=" + grant, { method: "POST", headers: headers(), body: JSON.stringify(body) }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error_description || j.msg || j.message || "Sign-in failed");
        return { access_token: j.access_token, refresh_token: j.refresh_token, expires_at: Date.now() + (j.expires_in || 3600) * 1000 };
      });
    });
  }
  function fresh() {
    if (!session) return Promise.reject(new Error("Not signed in"));
    if (session.expires_at - Date.now() > 60000) return Promise.resolve(session.access_token);
    return token("refresh_token", { refresh_token: session.refresh_token }).then(function (s) { saveSession(s); return s.access_token; });
  }
  function rpc(name, args) {
    return fresh().then(function (t) {
      return fetch(CFG.url + "/rest/v1/rpc/" + name, { method: "POST", headers: headers(t), body: JSON.stringify(args || {}) });
    }).then(function (r) {
      return r.text().then(function (txt) {
        var j; try { j = txt ? JSON.parse(txt) : null; } catch (e) { j = null; }
        if (!r.ok) { var m = new Error((j && (j.message || j.hint)) || txt || "Request failed"); m.status = r.status; throw m; }
        return j;
      });
    });
  }

  // ---- state ----------------------------------------------------------------------------------------
  var S = { data: null, team: null, sel: null, mull: null, tab: "lineup", week: null, match: null, offset: 0, busy: false };
  function say(text, err) { var m = $("msg"); m.textContent = text || ""; m.className = "msg" + (err ? " err" : ""); }
  function mine() { return S.data && S.data.team.id === S.data.me; }
  function locked() { return !S.data.open; }
  function zoneOf(p) { return p.slot === "ir" ? "ir" : p.starter ? "starters" : "bench"; }
  function inZone(z) { return S.data.players.filter(function (p) { return zoneOf(p) === z; }); }
  function byId(id) { return S.data.players.filter(function (p) { return p.id === id; })[0]; }
  function cap(z) { var L = S.data.limits; return z === "starters" ? L.starters : z === "bench" ? L.active - L.starters : L.ir; }
  var ZNAME = { starters: "Starters", bench: "Bench", ir: "IR" };

  function load(keepMsg) {
    var team = S.team;
    return rpc("screen_roster", team ? { p_team: team } : {}).then(function (d) {
      if (!d.team && d.teams && d.teams.length) return rpc("screen_roster", { p_team: d.teams[0].id });
      return d;
    }).then(function (d) {
      S.data = d; S.team = d.team.id; S.offset = new Date(d.now).getTime() - Date.now();
      if (!keepMsg) say("");
      return d;
    });
  }
  function loadMatch() {
    return rpc("screen_matchup", S.week ? { p_week: S.week } : {}).then(function (m) { S.match = m; if (!S.week) S.week = m.week; });
  }

  // ---- rules (the database enforces the same ones; these just explain before you tap) ----------
  function why(p, to, swapWith) {
    var from = zoneOf(p);
    if (from === to) return "Already there";
    if (locked() && (from === "starters" || to === "starters")) return "The starting lineup is locked until Tuesday at 3:00 a.m.";
    if (to === "ir" && !p.official) return "IR needs an official Out or injured-list designation";
    if (swapWith) {
      if (from === "ir" && !swapWith.official) return swapWith.name + " has no designation that allows IR";
      return "";
    }
    if (inZone(to).length >= cap(to)) return ZNAME[to] + " is full: swap with someone instead";
    return "";
  }
  function plan(p, to, swapWith) {
    // Final zones for the players involved, then the calls in a safe order: into IR, out of IR, then one lineup write.
    var fin = {}; fin[p.id] = to; if (swapWith) fin[swapWith.id] = zoneOf(p);
    var ops = [], ids = Object.keys(fin).map(Number);
    ids.forEach(function (id) { if (fin[id] === "ir" && zoneOf(byId(id)) !== "ir") ops.push(["move_to_ir", { p_player: id }]); });
    ids.forEach(function (id) { if (fin[id] !== "ir" && zoneOf(byId(id)) === "ir") ops.push(["return_from_ir", { p_player: id }]); });
    var starters = inZone("starters").map(function (x) { return x.id; });
    var next = starters.filter(function (id) { return !(id in fin) || fin[id] === "starters"; });
    ids.forEach(function (id) { if (fin[id] === "starters" && next.indexOf(id) < 0) next.push(id); });
    if (next.length !== starters.length || next.some(function (id) { return starters.indexOf(id) < 0; })) ops.push(["set_lineup", { p_players: next }]);
    return ops;
  }
  function perform(ops, okText) {
    if (S.busy) return;
    S.busy = true;
    var chain = Promise.resolve();
    ops.forEach(function (o) { chain = chain.then(function () { return rpc(o[0], o[1]); }); });
    return chain.then(function () { S.sel = null; S.mull = null; return load(true).then(function () { say(okText); render(); }); })
      .catch(function (e) { S.sel = null; S.mull = null; return load(true).then(function () { say(e.message, true); render(); }); })
      .then(function () { S.busy = false; });
  }

  // ---- rendering --------------------------------------------------------------------------------------
  function tag(s) { return '<span class="tag s-' + esc(s) + '">' + esc(s) + "</span>"; }
  function injTag(p) { return p.injury ? '<span class="inj' + (p.official ? "" : " soft") + '">' + esc(p.injury) + "</span>" : ""; }
  function rowHtml(p, zone) {
    var sel = S.sel === p.id, canEdit = mine(), acts = "", swapBtn = "";
    if (canEdit && S.sel && S.sel !== p.id && zoneOf(byId(S.sel)) !== zone) {
      var r = why(byId(S.sel), zone, p);
      swapBtn = '<button class="btn" data-swap="' + p.id + '"' + (r ? ' disabled title="' + esc(r) + '"' : "") + ">Swap</button>";
    }
    if (canEdit && S.mull) { swapBtn = zone === "bench" ? '<button class="btn primary" data-mullto="' + p.id + '">Swap in</button>' : ""; }
    if (canEdit && locked() && S.data.mulligan && !S.data.mulligan.used && zone === "starters" && p.official && !S.mull) acts = '<button class="btn" data-mull="' + p.id + '">Mulligan</button>';
    var inner = '<span class="who"><span class="nm">' + esc(p.name) + '</span><span class="meta">' + tag(p.sport) + "<span>" + esc(p.position) + "</span>" + (p.last != null ? "<span>Last week " + (Math.round(p.last * 10) / 10) + "</span>" : "") + injTag(p) + "</span></span>";
    var body = canEdit ? '<button class="pick" data-pick="' + p.id + '" aria-pressed="' + sel + '" aria-label="Select ' + esc(p.name) + '">' + inner + "</button>" : '<span class="who">' + inner + "</span>";
    return '<li class="row' + (sel ? " sel" : "") + '">' + body + '<span class="acts">' + acts + swapBtn + "</span></li>";
  }
  function listHtml(zone) {
    var rows = inZone(zone), h = rows.map(function (p) { return rowHtml(p, zone); }).join("");
    for (var i = rows.length; i < cap(zone); i++) h += '<li class="row empty"><span>Open ' + (zone === "ir" ? "IR spot" : zone === "bench" ? "bench spot" : "starter spot") + "</span><span></span></li>";
    return h;
  }
  function variety(n) { return n <= 0 ? 0 : Math.min(10, 2 * (n - 1)); }
  function fmtTime(ts) {
    return new Date(ts).toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit" }) + " ET";
  }
  function countdown(ms) {
    var m = Math.max(0, Math.round(ms / 60000)), h = Math.floor(m / 60);
    var u = function (n, w) { return n + " " + w + (n === 1 ? "" : "s"); };
    return h >= 24 ? u(Math.floor(h / 24), "day") + (h % 24 ? " " + u(h % 24, "hour") : "") : h ? u(h, "hour") + (m % 60 ? " " + u(m % 60, "minute") : "") : u(m, "minute");
  }
  function renderLineup() {
    var d = S.data, canEdit = mine(), rows = d.players;
    $("teamName").textContent = d.team.name + (canEdit ? " (you)" : "");
    var wk = d.open ? d.lineup_week : d.week;
    $("weekLine").textContent = "Week " + d.week + " now" + (d.open ? " · setting the lineup for week " + d.lineup_week : "");
    $("starters").innerHTML = listHtml("starters"); $("bench").innerHTML = listHtml("bench"); $("ir").innerHTML = listHtml("ir");
    var st = inZone("starters"), be = inZone("bench"), ir = inZone("ir");
    $("lineupCount").textContent = st.length + " starters · " + be.length + " bench · " + ir.length + " IR";
    var sc = {}; st.forEach(function (p) { sc[p.sport] = (sc[p.sport] || 0) + 1; });
    var n = Object.keys(sc).length;
    $("sportChips").innerHTML = SPORTS.filter(function (s) { return sc[s]; }).map(function (s) { return tag(s) + " " + sc[s]; }).join("  ") || '<span class="note">No starters</span>';
    $("varietyNow").textContent = "+" + variety(n) + " (" + n + (n === 1 ? " sport" : " sports") + ")";
    var nF1 = rows.filter(function (p) { return p.sport === "F1"; }).length;
    $("f1Count").textContent = d.limits.f1 < d.limits.active ? nF1 + " of " + d.limits.f1 : String(nF1);      // no "of N" when the league has no F1 limit
    $("activeCount").textContent = (st.length + be.length) + " of " + d.limits.active;
    $("startersHint").textContent = locked() ? "Locked until " + fmtTime(d.opens_at) : "10 spots. Only starters who play score.";
    var now = Date.now() + S.offset, lc = $("lockCard"), l = locked();
    lc.className = "lock" + (l ? " locked" : "");
    lc.innerHTML = l ? '<div class="eyebrow">Lineup status</div><b>Locked</b><div class="note">Reopens ' + esc(fmtTime(d.opens_at)) + " (in " + countdown(new Date(d.opens_at) - now) + ") for a nine-hour window before the noon lock. Bench and IR moves are still allowed.</div>"
      : '<div class="eyebrow">Lineup status</div><b>Locks in ' + countdown(new Date(d.locks_at) - now) + '</b><div class="note">Open until ' + esc(fmtTime(d.locks_at)) + ". Everyone in the league can see your lineup.</div>";
    var ab = $("actions");
    if (canEdit && S.sel && !S.mull) {
      var p = byId(S.sel), from = zoneOf(p), h = "<span><b>" + esc(p.name) + "</b> selected. Move to:</span>";
      ["starters", "bench", "ir"].forEach(function (z) {
        if (z === from) return;
        var r = inZone(z).length >= cap(z) ? "No open spot: tap Swap on a player instead" : why(p, z);
        h += '<button class="btn" data-moveto="' + z + '"' + (r ? ' disabled title="' + esc(r) + '"' : "") + ">" + ZNAME[z] + "</button>";
      });
      ab.innerHTML = h + '<button class="btn" data-cancel="1">Cancel</button>'; ab.hidden = false;
    } else if (canEdit && S.mull) { ab.innerHTML = "<span>Choose a bench player to swap in for <b>" + esc(byId(S.mull).name) + '</b>.</span><button class="btn" data-cancel="1">Cancel</button>'; ab.hidden = false; }
    else ab.hidden = true;
    if (!canEdit && !$("msg").textContent) say("Viewing " + d.team.name + ". Lineups are public, but only the owner can change one.");
    var mb = $("mullBody"), mu = d.mulligan;
    mb.innerHTML = '<p class="note" style="margin:0">' + (!canEdit ? "Only the owner can use a mulligan." : !mu ? "You do not hold a mulligan. The Cup runner-up earns one, used once after the lock to swap an injured starter for a bench player." : mu.used ? "Used. The bench player's full-week score replaces the injured starter's, and the variety bonus is recalculated." : locked() ? "Tap Mulligan next to an injured starter, then choose a bench player." : "Available after the lock, for a starter with an official injury designation.") + "</p>";
  }
  function fmt(n) { return (Math.round(Number(n) * 10) / 10).toFixed(1); }
  function renderMatch() {
    var m = S.match; if (!m) return;
    $("weekSel").value = String(S.week);
    if (m.none) { $("matchBanner").innerHTML = "<span><b>No game this week</b></span><span class='note' style='margin:0'>Week " + m.week + " has no matchup for your team yet.</span>"; $("matchCols").innerHTML = ""; return; }
    var a = m.sides[0], b = m.sides[1], diff = Number(a.total) - Number(b.total);
    var lead = m.final ? (m.winner === a.team ? "You won" : m.winner === b.team ? "You lost" : "Tied") : diff > 0 ? "You are leading" : diff < 0 ? "You are trailing" : "Dead even";
    var played = function (s) { return s.rows.filter(function (r) { return r.played; }).length; };
    $("matchBanner").innerHTML = "<span><b>" + lead + "</b> by " + fmt(Math.abs(diff)) + " points</span><span class='note' style='margin:0'>" + (m.final ? "Final" : "Live") + " · " + played(a) + " of " + a.rows.length + " of your starters and " + played(b) + " of " + b.rows.length + " of theirs have played</span>";
    function col(s, win) {
      var h = '<section class="panel"><div class="score' + (win ? " win" : "") + '"><div class="who">' + esc(s.name) + '</div><div class="big">' + fmt(s.total) + '</div><div class="note" style="margin:0">' + (win ? (m.final ? "Winner" : "Leading") : (m.final ? "" : "Trailing")) + "</div></div><div class='body'>";
      h += '<div class="stat"><span>Starters</span><b>' + fmt(s.starter_points) + '</b></div><div class="stat"><span>Variety bonus</span><b>+' + s.variety + '</b></div><div class="stat"><span>Bonus challenges</span><b>+' + fmt(s.bonus) + '</b></div><div class="stat"><span>Weekly total</span><b>' + fmt(s.total) + "</b></div>";
      h += '<ol class="list" style="margin-top:12px">' + (s.rows.length ? s.rows.map(function (r) {
        var cls = r.played ? (Number(r.score) < 0 ? "pts neg" : "pts") : "pts none";
        return '<li class="row"><span class="who"><span class="nm">' + esc(r.name) + '</span><span class="meta">' + tag(r.sport) + "<span>" + esc(r.position) + "</span>" + (r.played ? "" : "<span>Has not played</span>") + '</span></span><span class="' + cls + '">' + (r.played ? fmt(r.score) : "—") + "</span></li>";
      }).join("") : '<li class="row empty"><span>Lineup not visible yet</span><span></span></li>') + "</ol></div></section>";
      return h;
    }
    $("matchCols").innerHTML = col(a, m.final ? m.winner === a.team : diff > 0) + col(b, m.final ? m.winner === b.team : diff < 0);
  }
  function render() { renderLineup(); renderMatch(); }

  // ---- events --------------------------------------------------------------------------------------------
  $("view-lineup").addEventListener("click", function (e) {
    if (!S.data) return;
    var b, p;
    if ((b = e.target.closest("[data-pick]"))) { var id = +b.dataset.pick; S.sel = S.sel === id ? null : id; S.mull = null; say(""); renderLineup(); return; }
    if (e.target.closest("[data-cancel]")) { S.sel = null; S.mull = null; say(""); renderLineup(); return; }
    if ((b = e.target.closest("[data-moveto]")) && !b.disabled) {
      p = byId(S.sel); var to = b.dataset.moveto, r = why(p, to);
      if (r) { say(r, true); return; }
      perform(plan(p, to), p.name + " moved to " + ZNAME[to].toLowerCase() + "."); return;
    }
    if ((b = e.target.closest("[data-swap]")) && !b.disabled) {
      var other = byId(+b.dataset.swap); p = byId(S.sel); var r2 = why(p, zoneOf(other), other);
      if (r2) { say(r2, true); return; }
      perform(plan(p, zoneOf(other), other), p.name + " and " + other.name + " swapped places."); return;
    }
    if ((b = e.target.closest("[data-mull]"))) { S.mull = +b.dataset.mull; S.sel = null; say(""); renderLineup(); return; }
    if ((b = e.target.closest("[data-mullto]"))) {
      var out = byId(S.mull), inn = byId(+b.dataset.mullto);
      perform([["use_mulligan", { p_out: out.id, p_in: inn.id }]], inn.name + " replaces " + out.name + " for the week. Mulligan used.");
    }
  });
  $("teamSel").addEventListener("change", function (e) { S.team = +e.target.value; S.sel = null; S.mull = null; load().then(render).catch(function (x) { say(x.message, true); }); });
  $("weekSel").addEventListener("change", function (e) { S.week = +e.target.value; loadMatch().then(renderMatch).catch(function (x) { say(x.message, true); }); });

  // ---- tabs ------------------------------------------------------------------------------------------------
  var TABS = ["lineup", "matchup", "draft", "bonus", "standings", "players", "trades", "activity", "admin"];
  function showTab(name) {
    S.tab = name;
    TABS.forEach(function (n) { $("tab-" + n).setAttribute("aria-selected", String(n === name)); $("view-" + n).hidden = n !== name; });
    $("teamField").hidden = name !== "lineup"; $("weekField").hidden = name !== "matchup";
    var fail = function (x) { say(x.message, true); };
    if (name === "matchup") loadMatch().then(renderMatch).catch(fail);
    if (name === "players") loadMarket().then(renderPlayers).catch(function (x) { $("pmsg").textContent = x.message; $("pmsg").className = "msg err"; });
    if (name === "trades") Promise.all([loadMarket(), loadTrades()]).then(renderTrades).catch(function (x) { $("tmsg").textContent = x.message; $("tmsg").className = "msg err"; });
    if (name === "draft") loadDraft().then(renderDraft).catch(function (x) { dmsg(x.message, true); });
    if (name === "bonus") loadBonus().then(renderBonus).catch(function (x) { bmsg(x.message, true); });
    if (name === "standings") loadStandings().then(renderStandings).catch(function (x) { $("stmsg").textContent = x.message; $("stmsg").className = "msg err"; });
    if (name === "admin") loadAdmin().then(renderAdmin).catch(function (x) { $("amsg").textContent = x.message; $("amsg").className = "msg err"; });
    if (name === "activity") rpc("screen_activity", { p_limit: 80 }).then(renderActivity).catch(fail);
  }
  TABS.forEach(function (n) { $("tab-" + n).addEventListener("click", function () { showTab(n); }); });

  // ---- players, waivers, claims ------------------------------------------------------------------------
  var M = { data: null, addFor: null, drop: "", confirmDrop: null, sport: "", search: "" };
  var pmsg = function (t, err) { $("pmsg").textContent = t || ""; $("pmsg").className = "msg" + (err ? " err" : ""); };
  function loadMarket() {
    return rpc("screen_market", { p_sport: M.sport || null, p_search: M.search || null, p_limit: 60 }).then(function (d) { M.data = d; S.offset = new Date(d.now).getTime() - Date.now(); return d; });
  }
  function etFull(ts) { return new Date(ts).toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " ET"; }
  function activeCount() { return M.data.roster.filter(function (r) { return r.slot === "active"; }).length; }
  function dropOptions(mustDrop) {
    var full = activeCount() >= M.data.limits.active;
    return M.data.roster.filter(function (r) { return !r.locked && (!full || r.slot === "active"); });
  }
  function renderPlayers() {
    var d = M.data;
    $("pCount").textContent = d.players.length + " shown";
    var now = Date.now() + S.offset;
    $("runCard").className = "lock";
    $("runCard").innerHTML = '<div class="eyebrow">Next waiver run</div><b>' + esc(etFull(d.next_run)) + '</b><div class="note">Players who are dropped or newly signed are held first (1 day for drops, 2 days for new signings), then released at the next 9:00 a.m. ET. If that is a Tuesday, claims decide who gets him (worst record picks first, reset every week). Any other morning he is a free agent, first come, first served. A team that drops a player cannot add him back for 7 days.</div><div class="note">Active players: <b>' + activeCount() + " of " + d.limits.active + "</b></div>";
    $("pList").innerHTML = d.players.length ? d.players.map(function (p) {
      var status, btn = "";
      if (p.blocked_until) status = '<span class="chip warn">You dropped him: re-add after ' + esc(etFull(p.blocked_until)) + "</span>";
      else if (p.free) { status = '<span class="chip ok">Free agent</span>'; btn = '<button class="btn primary" data-add="' + p.id + '" data-kind="add">Add</button>'; }
      else if (p.on_waivers && p.claim_slot && new Date(p.release_at) <= now) status = '<span class="chip warn">Waiver run in progress</span>';
      else if (p.on_waivers && p.claim_slot) { status = p.claim_id ? '<span class="chip ok">Claim placed</span>' : '<span class="chip warn">On waivers: claims decided ' + esc(etFull(p.release_at)) + "</span>"; if (!p.claim_id) btn = '<button class="btn" data-add="' + p.id + '" data-kind="claim">Claim</button>'; }
      else if (p.on_waivers) status = '<span class="chip warn">On waivers until ' + esc(etFull(p.release_at)) + "</span>";
      else status = "";
      return '<li class="row"><span class="who"><span class="nm">' + esc(p.name) + '</span><span class="meta">' + tag(p.sport) + "<span>" + esc(p.position) + "</span>" + (p.recent != null ? "<span>Last scored " + fmt(p.recent) + "</span>" : "") + (p.injury ? '<span class="inj soft">' + esc(p.injury) + "</span>" : "") + status + '</span></span><span class="acts">' + btn + "</span></li>";
    }).join("") : '<li class="row empty"><span>No players match</span><span></span></li>';
    // add / claim box
    var box = $("addBox");
    if (M.addFor) {
      var p = d.players.filter(function (x) { return x.id === M.addFor.id; })[0];
      if (!p) { M.addFor = null; box.hidden = true; }
      else {
        var opts = dropOptions(), full = activeCount() >= d.limits.active, claim = M.addFor.kind === "claim";
        box.hidden = false;
        box.innerHTML = "<span><b>" + (claim ? "Claim " : "Add ") + esc(p.name) + "</b></span>" +
          '<label class="field" for="dropSel" style="display:inline-grid">' + (full ? "Roster is full: choose who to drop" : "Drop someone (optional)") +
          '<select id="dropSel" class="sel2">' + (full ? '<option value="">Choose a player to drop</option>' : '<option value="">No drop</option>') +
          opts.map(function (r) { return '<option value="' + r.id + '"' + (String(r.id) === M.drop ? " selected" : "") + ">" + esc(r.name) + " (" + (r.slot === "ir" ? "IR" : r.in_lineup ? "starter" : "bench") + ")</option>"; }).join("") + "</select></label>" +
          '<button class="btn primary" data-confirmadd="1">' + (claim ? "Place claim" : "Add player") + '</button><button class="btn" data-canceladd="1">Cancel</button>';
      }
    } else box.hidden = true;
    // claims
    var cl = d.claims, pend = cl.filter(function (c) { return c.status === "pending"; });
    $("claimsBody").innerHTML = cl.length ? '<ol class="list">' + cl.map(function (c) {
      var i = pend.indexOf(c);
      var st = c.status === "pending" ? '<span class="chip warn">Pending, run ' + esc(etFull(c.run_at)) + "</span>" : c.status === "won" ? '<span class="chip ok">Won</span>' : '<span class="chip bad">' + (c.status === "lost" ? "Lost" : "Void") + "</span>";
      return '<li class="row"><span class="who"><span class="nm">' + (c.status === "pending" ? "#" + c.rank + " " : "") + esc(c.name) + '</span><span class="meta">' + st + (c.drop_name ? "<span>Drop " + esc(c.drop_name) + "</span>" : "") + (c.reason ? "<span>" + esc(c.reason) + "</span>" : "") + '</span></span><span class="acts">' +
        (c.status === "pending" ? '<button class="btn" data-up="' + c.id + '" aria-label="Move claim up"' + (i === 0 ? " disabled" : "") + '>↑</button><button class="btn" data-down="' + c.id + '" aria-label="Move claim down"' + (i === pend.length - 1 ? " disabled" : "") + '>↓</button><button class="btn" data-cancelclaim="' + c.id + '">Cancel</button>' : "") + "</span></li>";
    }).join("") + '</ol><p class="note">If you win a claim, your other claims for that run stay in play in your order. Each win sends you to the back of the line for the rest of the run.</p>' : '<p class="note" style="margin:0">No claims. Claim a player who is on waivers for the Tuesday run.</p>';
    // roster
    $("rosterList").innerHTML = d.roster.map(function (r) {
      var why = r.locked ? "In the locked lineup until Tuesday at 3:00 a.m." : "";
      var act = M.confirmDrop === r.id ? '<button class="btn" data-dropdo="' + r.id + '" style="border-color:var(--bad);color:var(--bad)">Confirm drop</button><button class="btn" data-dropno="1">Keep</button>' : '<button class="btn" data-drop="' + r.id + '"' + (why ? ' disabled title="' + esc(why) + '"' : "") + ">Drop</button>";
      return '<li class="row"><span class="who"><span class="nm">' + esc(r.name) + '</span><span class="meta">' + tag(r.sport) + "<span>" + esc(r.position) + "</span>" + (r.slot === "ir" ? "<span>IR</span>" : r.in_lineup ? "<span>Starter</span>" : "") + (r.injury ? '<span class="inj soft">' + esc(r.injury) + "</span>" : "") + '</span></span><span class="acts">' + act + "</span></li>";
    }).join("");
  }
  function marketDo(call, okText) {
    if (S.busy) return Promise.resolve();
    S.busy = true;
    return call().then(function () { M.addFor = null; M.drop = ""; M.confirmDrop = null; return loadMarket().then(function () { pmsg(okText); renderPlayers(); }); })
      .catch(function (e) { return loadMarket().then(function () { pmsg(e.message, true); renderPlayers(); }); })
      .then(function () { S.busy = false; });
  }
  $("view-players").addEventListener("click", function (e) {
    var b;
    if ((b = e.target.closest("[data-add]"))) { M.addFor = { id: +b.dataset.add, kind: b.dataset.kind }; M.drop = ""; pmsg(""); renderPlayers(); return; }
    if (e.target.closest("[data-canceladd]")) { M.addFor = null; renderPlayers(); return; }
    if (e.target.closest("[data-confirmadd]")) {
      var drop = $("dropSel").value ? +$("dropSel").value : null, a = M.addFor, p = M.data.players.filter(function (x) { return x.id === a.id; })[0];
      marketDo(function () { return rpc(a.kind === "claim" ? "place_claim" : "add_free_agent", { p_player: a.id, p_drop: drop }); }, a.kind === "claim" ? "Claim placed for " + p.name + "." : p.name + " added to your roster."); return;
    }
    if ((b = e.target.closest("[data-drop]"))) { M.confirmDrop = +b.dataset.drop; renderPlayers(); return; }
    if (e.target.closest("[data-dropno]")) { M.confirmDrop = null; renderPlayers(); return; }
    if ((b = e.target.closest("[data-dropdo]"))) { var id = +b.dataset.dropdo, nm = M.data.roster.filter(function (r) { return r.id === id; })[0].name; marketDo(function () { return rpc("drop_player", { p_player: id }); }, nm + " dropped. He is on waivers; you cannot add him back for 7 days."); return; }
    if ((b = e.target.closest("[data-cancelclaim]"))) { marketDo(function () { return rpc("cancel_claim", { p_id: +b.dataset.cancelclaim }); }, "Claim cancelled."); return; }
    if ((b = e.target.closest("[data-up],[data-down]"))) {
      var cid = +(b.dataset.up || b.dataset.down), ids = M.data.claims.filter(function (c) { return c.status === "pending"; }).map(function (c) { return c.id; }), i = ids.indexOf(cid), j = b.dataset.up ? i - 1 : i + 1;
      ids.splice(j, 0, ids.splice(i, 1)[0]);
      marketDo(function () { return rpc("reorder_claims", { p_ids: ids }); }, "Claims reordered.");
    }
  });
  $("view-players").addEventListener("change", function (e) {
    if (e.target.id === "dropSel") { M.drop = e.target.value; return; }
    if (e.target.id === "pSport") { M.sport = e.target.value; loadMarket().then(renderPlayers).catch(function (x) { pmsg(x.message, true); }); }
  });
  var searchTimer;
  $("pSearch").addEventListener("input", function (e) { clearTimeout(searchTimer); searchTimer = setTimeout(function () { M.search = e.target.value.trim(); loadMarket().then(renderPlayers).catch(function (x) { pmsg(x.message, true); }); }, 250); });

  // ---- trades ---------------------------------------------------------------------------------------------------
  var TR = { data: null, partner: null, proot: null, give: {}, get: {}, myDrops: {}, theirDrops: {} };
  var tmsg = function (t, err) { $("tmsg").textContent = t || ""; $("tmsg").className = "msg" + (err ? " err" : ""); };
  function loadTrades() { return rpc("screen_trades").then(function (d) { TR.data = d; return d; }); }
  function loadPartner() {
    if (!TR.partner) { TR.proot = null; return Promise.resolve(); }
    return rpc("screen_roster", { p_team: TR.partner }).then(function (d) { TR.proot = d; });
  }
  function names(list) { return list.length ? list.map(function (p) { return esc(p.name) + ' <span class="note" style="margin:0">' + esc(p.position) + "</span>"; }).join("<br>") : "<span class='note' style='margin:0'>nothing</span>"; }
  var STATUS = { proposed: ["warn", "Waiting for a reply"], pending_veto: ["warn", "Accepted: veto window"], completed: ["ok", "Completed"], vetoed: ["bad", "Vetoed"], declined: ["bad", "Declined"], withdrawn: ["bad", "Withdrawn"], expired: ["bad", "Expired"], failed: ["bad", "Failed"] };
  function renderTrades() {
    var d = TR.data, me = d.me;
    $("tDeadline").textContent = d.open ? "Deadline " + etFull(d.deadline) : "Trade deadline has passed";
    var mine = d.trades.filter(function (t) { return t.proposer === me || t.partner === me; }), others = d.trades.filter(function (t) { return t.proposer !== me && t.partner !== me; });
    function card(t) {
      var st = STATUS[t.status] || ["", t.status], acts = "", info = "";
      if (t.status === "proposed" && t.partner === me) acts = '<button class="btn primary" data-accept="' + t.id + '">Accept</button><button class="btn" data-decline="' + t.id + '">Decline</button>';
      if (t.status === "proposed" && t.proposer === me) acts = '<button class="btn" data-withdraw="' + t.id + '">Withdraw</button>';
      if (t.status === "pending_veto") info = "Completes " + etFull(t.veto_until) + " unless the commissioner vetoes it. Acquired players go to the bench.";
      if (t.note) info = (t.status === "vetoed" ? "Veto reason: " : "") + t.note;
      var drops = t.drops.length ? '<div class="note" style="margin:0">Drops: ' + t.drops.map(function (x) { return esc(x.name); }).join(", ") + "</div>" : "";
      return '<li class="trade"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><b>Trade #' + t.id + " · " + esc(t.proposer_name) + " and " + esc(t.partner_name) + '</b><span class="chip ' + st[0] + '">' + esc(st[1]) + '</span></div><div class="sides"><div><div class="eyebrow">' + esc(t.proposer_name) + " sends</div>" + names(t.give) + '</div><div><div class="eyebrow">' + esc(t.partner_name) + " sends</div>" + names(t.get) + "</div></div>" + drops + (info ? '<div class="note" style="margin:0">' + esc(info) + "</div>" : "") + (acts ? '<div class="acts">' + acts + "</div>" : "") + "</li>";
    }
    $("tList").innerHTML = (mine.length ? "<li class='eyebrow'>Your trades</li>" + mine.map(card).join("") : "<li class='note'>You have no trades yet. Propose one on the right.</li>") + (others.length ? "<li class='eyebrow' style='margin-top:8px'>Completed around the league</li>" + others.map(card).join("") : "");
    renderPropose();
  }
  function ckList(kind, list, chosen, why) {
    return list.map(function (p) {
      var r = why(p);
      return '<label class="chk" data-off="' + (r ? 1 : 0) + '"><input type="checkbox" data-' + kind + '="' + p.id + '"' + (chosen[p.id] ? " checked" : "") + (r ? " disabled" : "") + '><span>' + esc(p.name) + ' <span class="note" style="margin:0">' + esc(p.sport) + " " + esc(p.position) + (r ? " · " + esc(r) : "") + "</span></span></label>";
    }).join("");
  }
  function renderPropose() {
    var d = TR.data, md = M.data, el = $("proposeBody");
    if (!d.open) { el.innerHTML = '<p class="note" style="margin:0">Trades closed at the end of week 44.</p>'; return; }
    var h = '<label class="field" for="tPartner">Trade with<select id="tPartner" class="sel2"><option value="">Choose a team</option>' + (md ? "" : "") + (TR.teamList || []).filter(function (t) { return t.id !== d.me; }).map(function (t) { return '<option value="' + t.id + '"' + (TR.partner === t.id ? " selected" : "") + ">" + esc(t.name) + "</option>"; }).join("") + "</select></label>";
    if (TR.partner && TR.proot && md) {
      var mineP = md.roster, theirs = TR.proot.players;
      var inLineup = function (p) { return p.in_lineup || p.locked ? "In your lineup: take him out first" : ""; };
      var theirLineup = function (p) { return p.starter ? "In their lineup" : ""; };
      var sendIds = Object.keys(TR.give).filter(function (k) { return TR.give[k]; }).map(Number), getIds = Object.keys(TR.get).filter(function (k) { return TR.get[k]; }).map(Number);
      h += '<div class="zone" style="margin-top:12px"><h3>You send</h3><div class="list" style="display:grid;gap:6px">' + ckList("give", mineP, TR.give, inLineup) + "</div></div>";
      h += '<div class="zone"><h3>You get</h3><div class="list" style="display:grid;gap:6px">' + ckList("get", theirs, TR.get, theirLineup) + "</div></div>";
      var lim = md.limits.active, myAfter = mineP.filter(function (p) { return p.slot === "active" && sendIds.indexOf(p.id) < 0; }).length + getIds.length;
      var theirAfter = theirs.filter(function (p) { return p.slot === "active" && getIds.indexOf(p.id) < 0; }).length + sendIds.length;
      var myNeed = Math.max(0, myAfter - lim), theirNeed = Math.max(0, theirAfter - lim);
      if (myNeed) h += '<div class="zone"><h3>You must drop ' + myNeed + '</h3><div class="list" style="display:grid;gap:6px">' + ckList("mydrop", mineP.filter(function (p) { return p.slot === "active" && sendIds.indexOf(p.id) < 0; }), TR.myDrops, function (p) { return p.locked ? "In the locked lineup" : ""; }) + "</div></div>";
      if (theirNeed) h += '<div class="zone"><h3>' + esc(TR.proot.team.name) + " must drop " + theirNeed + '</h3><div class="list" style="display:grid;gap:6px">' + ckList("theirdrop", theirs.filter(function (p) { return p.slot === "active" && getIds.indexOf(p.id) < 0; }), TR.theirDrops, theirLineup) + "</div></div>";
      h += '<div class="msg" id="tpMsg"></div><button class="btn primary" id="tSend"' + (sendIds.length && getIds.length ? "" : " disabled") + '>Propose trade</button><p class="note">The other team accepts, then the commissioner has 24 hours to veto before it completes. Players you receive start on the bench.</p>';
    }
    el.innerHTML = h;
  }
  function resetPropose() { TR.give = {}; TR.get = {}; TR.myDrops = {}; TR.theirDrops = {}; }
  $("view-trades").addEventListener("change", function (e) {
    var t = e.target;
    if (t.id === "tPartner") { TR.partner = t.value ? +t.value : null; resetPropose(); loadPartner().then(renderPropose).catch(function (x) { tmsg(x.message, true); }); return; }
    var map = { give: "give", get: "get", mydrop: "myDrops", theirdrop: "theirDrops" };
    Object.keys(map).forEach(function (k) { if (t.dataset && t.dataset[k]) { TR[map[k]][t.dataset[k]] = t.checked; renderPropose(); } });
  });
  function tradeDo(call, okText) {
    if (S.busy) return;
    S.busy = true;
    call().then(function () { resetPropose(); return Promise.all([loadMarket(), loadTrades()]).then(loadPartner).then(function () { renderTrades(); tmsg(okText); }); })
      .catch(function (e) { return Promise.all([loadMarket(), loadTrades()]).then(function () { renderTrades(); tmsg(e.message, true); }); })
      .then(function () { S.busy = false; });
  }
  $("view-trades").addEventListener("click", function (e) {
    var b, ids = function (o) { return Object.keys(o).filter(function (k) { return o[k]; }).map(Number); };
    if (e.target.id === "tSend") {
      var partner = TR.partner;
      tradeDo(function () { return rpc("propose_trade", { p_partner: partner, p_give: ids(TR.give), p_get: ids(TR.get), p_my_drops: ids(TR.myDrops), p_their_drops: ids(TR.theirDrops) }); }, "Trade proposed. " + esc(TR.proot.team.name) + " has to accept it."); return;
    }
    if ((b = e.target.closest("[data-accept]"))) { tradeDo(function () { return rpc("accept_trade", { p_id: +b.dataset.accept }); }, "Trade accepted. The commissioner has 24 hours to veto it."); return; }
    if ((b = e.target.closest("[data-decline]"))) { tradeDo(function () { return rpc("decline_trade", { p_id: +b.dataset.decline }); }, "Trade declined."); return; }
    if ((b = e.target.closest("[data-withdraw]"))) { tradeDo(function () { return rpc("withdraw_trade", { p_id: +b.dataset.withdraw }); }, "Trade withdrawn."); }
  });

  // ---- activity ---------------------------------------------------------------------------------------------------
  function renderActivity(rows) {
    $("actList").innerHTML = rows.length ? rows.map(function (a) {
      return '<li class="row"><span class="who"><span class="nm">' + esc(a.message) + '</span><span class="meta"><span>' + esc(etFull(a.at)) + "</span>" + (a.commissioner ? '<span class="chip warn">Commissioner</span>' : "") + "</span></span><span></span></li>";
    }).join("") : '<li class="row empty"><span>Nothing yet</span><span></span></li>';
  }


  // ---- commissioner ------------------------------------------------------------------------------------------------
  var A = { data: null, found: null, pick: null };
  var amsg = function (t, err) { $("amsg").textContent = t || ""; $("amsg").className = "msg" + (err ? " err" : ""); };
  // Heartbeats of the scheduled jobs: a badge for everyone, the full picture for the commissioner.
  var H = { data: null };
  var JOB_STATUS = { ok: ["ok", "On time"], waiting: ["", "Not started yet"], late: ["warn", "Late"], down: ["bad", "Stopped"] };
  function ago(min) { return min == null ? "never" : min < 1 ? "just now" : min < 90 ? Math.round(min) + " min ago" : min < 2880 ? Math.round(min / 60) + " h ago" : Math.round(min / 1440) + " days ago"; }
  function loadHealth() {
    return rpc("screen_health").then(function (h) {
      H.data = h; var st = h.scores.status, b = $("liveBadge");
      b.textContent = st === "late" ? "Scores delayed" : st === "down" ? "Scores paused" : "Live";
      b.className = "badge" + (st === "late" ? " warn" : st === "down" ? " bad" : "");
      b.title = h.scores.minutes != null ? "Scores last updated " + ago(h.scores.minutes) : "";
      if (h.commissioner) renderHealth();
    }).catch(function () {});
  }
  function renderHealth() {
    var h = H.data; if (!h || !h.commissioner) return;
    var f = h.facts, worst = h.jobs.some(function (j) { return j.status === "down"; }) ? "down" : h.jobs.some(function (j) { return j.status === "late"; }) ? "late" : "ok";
    $("healthNote").textContent = worst === "ok" ? "All jobs on time" : worst === "late" ? "A job is running late" : "A job has stopped";
    $("healthList").innerHTML = h.jobs.map(function (j) {
      var s = JOB_STATUS[j.status], min = j.last_ok_at ? (new Date(h.now).getTime() - new Date(j.last_ok_at).getTime()) / 60000 : null;
      return '<li class="row" style="display:grid;gap:2px"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><b>' + esc(j.label) + '</b><span class="chip ' + s[0] + '">' + s[1] + '</span></div>' +
        '<div class="note" style="margin:0">Expected ' + esc(j.expect) + ". Last good run: " + ago(min) + ".</div>" +
        (j.error ? '<div class="note" style="margin:0;color:var(--bad)">Last run failed: ' + esc(j.error) + "</div>" : "") + "</li>";
    }).join("");
    $("healthFacts").textContent = f.teams_with_owner + " of " + f.teams + " teams have a manager signed up · " + f.players + " players in the pool · week " + f.week +
      (f.live_draft ? " · a draft is live" : "") + (f.pending_trades ? " · " + f.pending_trades + " trade(s) waiting" : "");
  }
  function loadAdmin() { return rpc("screen_admin").then(function (d) { A.data = d; S.offset = new Date(d.now).getTime() - Date.now(); return d; }); }
  function sideList(l) { return l.length ? l.map(function (p) { return esc(p.name) + ' <span class="note" style="margin:0">' + esc(p.position) + "</span>"; }).join("<br>") : "nothing"; }
  function renderAdmin() {
    var d = A.data, now = Date.now() + S.offset;
    if ($("cWeek").options.length === 0) { $("cWeek").innerHTML = Array.from({ length: 51 }, function (_, i) { return "<option value='" + (i + 1) + "'>Week " + (i + 1) + "</option>"; }).join(""); $("cWeek").value = String(d.week); }
    $("mTeam").innerHTML = d.teams.filter(function (t) { return !t.mulligan; }).map(function (t) { return '<option value="' + t.id + '">' + esc(t.name) + "</option>"; }).join("") || "<option value=''>Every team has one</option>";
    $("vetoList").innerHTML = d.veto.length ? d.veto.map(function (t) {
      var left = Math.max(0, new Date(t.veto_until).getTime() - now), h = Math.floor(left / 3600000), m = Math.floor(left % 3600000 / 60000);
      return '<li class="trade"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><b>Trade #' + t.id + " · " + esc(t.proposer_name) + " and " + esc(t.partner_name) + '</b><span class="chip warn">' + h + " h " + m + ' m left</span></div>' +
        '<div class="sides"><div><div class="eyebrow">' + esc(t.proposer_name) + " sends</div>" + sideList(t.give) + '</div><div><div class="eyebrow">' + esc(t.partner_name) + " sends</div>" + sideList(t.get) + "</div></div>" +
        (t.drops.length ? '<div class="note" style="margin:0">Drops: ' + t.drops.map(function (x) { return esc(x.name); }).join(", ") + "</div>" : "") +
        '<div class="acts"><label class="field" style="flex:1;min-width:180px">Reason (required to veto)<input data-vreason="' + t.id + '" style="padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);font:inherit;color:inherit"></label><button class="btn" data-veto="' + t.id + '" style="border-color:var(--bad);color:var(--bad)">Veto</button></div></li>';
    }).join("") : '<li class="note">No trades are waiting. A trade appears here for 24 hours after the other team accepts it.</li>';
    $("corrList").innerHTML = d.corrections.length ? d.corrections.map(function (c) {
      return '<li class="row"><span class="who"><span class="nm">#' + c.id + " " + esc(c.player) + " · week " + c.week + " · " + esc(c.stat) + '</span><span class="meta"><span>' + (c.old == null ? "none" : c.old) + " to " + (c.new == null ? "none" : c.new) + "</span><span>" + esc(c.reason) + "</span>" + (c.reverted ? '<span class="chip bad">Reverted</span>' : "") + (c.is_revert ? '<span class="chip">Revert</span>' : "") + '</span></span><span class="acts">' +
        (!c.is_revert && !c.reverted && !c.final ? '<button class="btn" data-revert="' + c.id + '">Revert</button>' : "") + "</span></li>";
    }).join("") : '<li class="row empty"><span>No corrections yet</span><span></span></li>';
    $("adList").innerHTML = d.drafts.length ? d.drafts.map(function (x) {
      return '<li class="row"><span class="who"><span class="nm">' + (x.kind === "main" ? "Main draft" : esc(x.event || "Bonus draft")) + " #" + x.id + '</span><span class="meta"><span>' + x.rounds + " rounds</span>" + (x.status === "live" ? '<span class="chip warn">Live</span>' : x.status === "done" ? '<span class="chip ok">Done</span>' : '<span class="chip">Scheduled</span>') + '</span></span><span class="acts">' + (x.status === "scheduled" ? '<button class="btn primary" data-dstart="' + x.id + '">Start draft</button>' : "") + "</span></li>";
    }).join("") : '<li class="note">No drafts yet.</li>';
    $("adCreate").hidden = d.drafts.some(function (x) { return x.kind === "main"; });
    $("abList").innerHTML = d.events.length ? d.events.map(function (e) {
      var st = BSTATE[e.state] || ["", e.state];
      return '<li class="row"><span class="who"><span class="nm">' + esc(e.name) + '</span><span class="meta"><span class="chip ' + st[0] + '">' + st[1] + "</span><span>" + e.entrants + " entrants, " + e.results + " results</span>" + (e.draft ? "<span>Draft " + e.draft.status + "</span>" : "") + '</span></span><span class="acts">' +
        (!e.draft ? '<button class="btn" data-bdcreate="' + e.id + '">Create draft</button>' : "") + '<button class="btn" data-bres="' + e.id + '">Enter results</button>' +
        (e.hold && e.state === "held" ? '<button class="btn" data-bconclude="' + e.id + '">Conclude</button>' : "") + "</span></li>";
    }).join("") : '<li class="note">No events yet.</li>';
    renderFound();
  }
  function renderFound() {
    var f = A.found;
    $("cPlayers").innerHTML = f ? (f.players.length ? f.players.map(function (p) {
      return '<li class="row"><span class="who"><span class="nm">' + esc(p.name) + '</span><span class="meta">' + tag(p.sport) + "<span>" + esc(p.position) + "</span>" + Object.keys(p.stats).map(function (k) { return "<span>" + esc(k) + " " + p.stats[k] + "</span>"; }).join("") + '</span></span><span class="acts"><button class="btn" data-cpick="' + p.id + '">Correct</button></span></li>';
    }).join("") : '<li class="row empty"><span>No players match</span><span></span></li>') : "";
    var form = $("cForm"), p = f && A.pick ? f.players.filter(function (x) { return x.id === A.pick; })[0] : null;
    if (!p) { form.hidden = true; A.formKey = ""; return; }
    var key = p.id + ":" + f.week + ":" + f.final;
    if (A.formKey === key && !form.hidden) return;       // keep what the commissioner has typed
    A.formKey = key; form.hidden = false;
    form.innerHTML = '<b>' + esc(p.name) + ", week " + f.week + "</b>" + (f.final ? '<div class="msg err">This week is final; corrections are ignored.</div>' : "") +
      '<label class="field" for="cStat">Stat<input id="cStat" list="cStats" placeholder="e.g. Points" style="padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);font:inherit;color:inherit"><datalist id="cStats">' + Object.keys(p.stats).map(function (k) { return "<option value='" + esc(k) + "'>"; }).join("") + "</datalist></label>" +
      '<label class="field" for="cValue">New value<input id="cValue" type="number" step="any" style="padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);font:inherit;color:inherit"></label>' +
      '<label class="field" for="cReason">Reason<input id="cReason" style="padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);font:inherit;color:inherit"></label>' +
      '<button class="btn primary" id="cApply">Apply correction</button>';
  }
  function findPlayers() {
    return rpc("admin_player_stats", { p_search: $("cSearch").value.trim(), p_week: +$("cWeek").value }).then(function (f) { A.found = f; renderFound(); }).catch(function (x) { amsg(x.message, true); });
  }
  var cTimer;
  $("cSearch").addEventListener("input", function () { clearTimeout(cTimer); cTimer = setTimeout(function () { A.pick = null; if ($("cSearch").value.trim()) findPlayers(); else { A.found = null; renderFound(); } }, 250); });
  $("cWeek").addEventListener("change", function () { if ($("cSearch").value.trim()) findPlayers(); });
  function adminDo(call, okText, onOk) {
    if (S.busy) return; S.busy = true;
    call().then(function () { A.pick = null; if (onOk) onOk(); return loadAdmin().then(function () { renderAdmin(); amsg(okText); }); })
      .catch(function (e) { return loadAdmin().then(function () { renderAdmin(); amsg(e.message, true); }); })
      .then(function () { S.busy = false; if ($("cSearch").value.trim()) findPlayers(); });
  }
  $("view-admin").addEventListener("click", function (e) {
    var b;
    if ((b = e.target.closest("[data-veto]"))) { var id = +b.dataset.veto, r = document.querySelector('[data-vreason="' + id + '"]').value; adminDo(function () { return rpc("veto_trade", { p_id: id, p_reason: r }); }, "Trade #" + id + " vetoed. Both teams keep their players."); return; }
    if ((b = e.target.closest("[data-revert]"))) { var cid = +b.dataset.revert; adminDo(function () { return rpc("revert_correction", { p_id: cid }); }, "Correction #" + cid + " reverted."); return; }
    if ((b = e.target.closest("[data-cpick]"))) { A.pick = +b.dataset.cpick; renderFound(); return; }
    if (e.target.id === "cApply") {
      var args = { p_player: A.pick, p_week: +$("cWeek").value, p_stat: $("cStat").value.trim(), p_value: $("cValue").value === "" ? null : +$("cValue").value, p_reason: $("cReason").value };
      adminDo(function () { return rpc("apply_correction", args); }, "Correction applied. Scores update at the next scoring run."); return;
    }
    if (e.target.id === "abCreate") {
      var ends = $("abEnds").value; if (!ends) { amsg("Choose when the event ends.", true); return; }
      var field = $("abEntrants").value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
      adminDo(function () { return rpc("create_bonus_event", { p_name: $("abName").value, p_ends_at: ends.replace("T", " ") + " America/New_York", p_hold: $("abHold").checked, p_entrants: field }); }, "Event added.", function () { ["abName", "abEnds", "abEntrants"].forEach(function (i) { $(i).value = ""; }); $("abHold").checked = false; }); return;
    }
    if ((b = e.target.closest("[data-bdcreate]"))) { var eid = +b.dataset.bdcreate; adminDo(function () { return rpc("create_bonus_draft", { p_event: eid }); }, "Draft created (2 rounds). Start it from the Draft list above."); return; }
    if ((b = e.target.closest("[data-bconclude]"))) { var cid2 = +b.dataset.bconclude; adminDo(function () { return rpc("conclude_bonus_event", { p_event: cid2 }); }, "Event concluded; held points released."); return; }
    if ((b = e.target.closest("[data-bres]"))) { openResults(+b.dataset.bres); return; }
    if (e.target.id === "abSave") { saveResults(); return; }
    if (e.target.id === "abClose") { $("abResults").hidden = true; return; }
    if (e.target.id === "adCreate") { adminDo(function () { return rpc("create_draft", { p_kind: "main", p_rounds: 20 }); }, "Main draft created. Start it when everyone is ready."); return; }
    if ((b = e.target.closest("[data-dstart]"))) { var did = +b.dataset.dstart; adminDo(function () { return rpc("start_draft", { p_id: did }); }, "The draft is live."); return; }
    if (e.target.id === "mGrant") { var t = $("mTeam").value; if (!t) return; adminDo(function () { return rpc("grant_mulligan", { p_team: +t }); }, "Mulligan granted."); }
  });



  // ---- bonus challenges (everyone) -------------------------------------------------------------------------------------
  var B = { list: null, detail: null, event: null };
  var bmsg = function (t, err) { $("bmsg").textContent = t || ""; $("bmsg").className = "msg" + (err ? " err" : ""); };
  var BSTATE = { open: ["ok", "Open"], held: ["warn", "Points held"], scored: ["", "Scored"] };
  function loadBonus() { return rpc("screen_bonus", B.event ? { p_event: B.event } : {}).then(function (d) { B.list = d; S.offset = new Date(d.now).getTime() - Date.now(); return d; }); }
  function pts(v) { return Number(v).toFixed(1); }
  function renderBonus() {
    var d = B.list;
    $("bnListPanel").hidden = !!B.event; $("bnDetail").hidden = !B.event;
    $("bnList").innerHTML = d.events.length ? d.events.map(function (e) {
      var st = BSTATE[e.state] || ["", e.state], dr = e.draft, draftChip = !dr ? '<span class="chip">No draft yet</span>' : dr.status === "live" ? '<span class="chip warn">Draft is live</span>' : dr.status === "scheduled" ? '<span class="chip">Draft scheduled</span>' : '<span class="chip ok">Drafted</span>';
      return '<li class="row"><span class="who"><span class="nm">' + esc(e.name) + '</span><span class="meta"><span>Ends ' + esc(etFull(e.ends_at)) + '</span><span class="chip ' + st[0] + '">' + st[1] + "</span>" + draftChip + "<span>" + e.entrants + " entrants</span>" + (e.my_picks ? "<span>You: " + e.my_picks + " picks, " + pts(e.my_points) + " pts</span>" : "") + '</span></span><span class="acts">' +
        (dr && dr.status !== "done" ? '<button class="btn primary" data-bdraft="' + dr.id + '">' + (dr.status === "live" ? "Join draft" : "Draft room") + "</button>" : "") + '<button class="btn" data-bopen="' + e.id + '">Results</button></span></li>';
    }).join("") : '<li class="note">No bonus events yet. The commissioner adds them.</li>';
    if (B.event && d.detail) {
      var ev = d.events.filter(function (x) { return x.id === B.event; })[0] || { name: "Event", state: "open" }, q = ($("bnSearch").value || "").toLowerCase();
      $("bnTitle").textContent = ev.name;
      $("bnStatus").textContent = ev.state === "held" ? "Points are held until the commissioner concludes the event." : ev.state === "scored" ? "Scored." : "Open. Ends " + etFull(ev.ends_at) + ".";
      $("bnTeams").innerHTML = "<thead><tr><th>Team</th><th class='n'>Bonus points</th></tr></thead><tbody>" + d.detail.teams.map(function (t) { return "<tr class='" + (t.id === d.me ? "me" : "") + "'><td>" + esc(t.name) + "</td><td class='n'>" + pts(t.points) + "</td></tr>"; }).join("") + "</tbody>";
      var rows = d.detail.entrants.filter(function (n) { return !q || n.name.toLowerCase().indexOf(q) >= 0; });
      $("bnEntrants").innerHTML = "<thead><tr><th class='n'>Place</th><th>Entrant</th><th>Picked by</th><th class='n'>Points</th></tr></thead><tbody>" + rows.map(function (n) {
        return "<tr class='" + (n.team_id && n.team_id === d.me ? "me" : "") + "'><td class='n'>" + (n.place != null ? n.place : n.status ? esc(n.status) : "–") + "</td><td>" + esc(n.name) + "</td><td>" + (n.team ? esc(n.team) : "") + "</td><td class='n'>" + (n.team ? pts(n.points) : "") + "</td></tr>";
      }).join("") + "</tbody>";
    }
  }
  $("view-bonus").addEventListener("click", function (e) {
    var b;
    if ((b = e.target.closest("[data-bopen]"))) { B.event = +b.dataset.bopen; $("bnSearch").value = ""; loadBonus().then(renderBonus).catch(function (x) { bmsg(x.message, true); }); return; }
    if (e.target.id === "bnBack") { B.event = null; loadBonus().then(renderBonus); return; }
    if ((b = e.target.closest("[data-bdraft]"))) { D.id = +b.dataset.bdraft; showTab("draft"); }
  });
  $("bnSearch").addEventListener("input", function () { if (B.list) renderBonus(); });


  // bonus results entry: one line per entrant, "Name: place" or "Name: Missed cut"
  var STATUS_WORDS = { "missed cut": "Missed cut", mc: "Missed cut", withdrew: "Withdrew", wd: "Withdrew", "did not start": "Did not start", dns: "Did not start", "not classified": "Not classified", nc: "Not classified" };
  function parseResults(text) {
    var out = [], bad = [];
    text.split("\n").forEach(function (line, i) {
      line = line.trim(); if (!line) return;
      var m = line.match(/^(.*?)\s*[:|\t]\s*([^:|\t]+)$/);
      if (!m) { bad.push("Line " + (i + 1) + ": use Name: place"); return; }
      var v = m[2].trim(), n = /^\d+$/.test(v) ? +v : null, st = n != null ? "Placed" : STATUS_WORDS[v.toLowerCase()];
      if (!st || (n != null && n < 1)) { bad.push("Line " + (i + 1) + ": \"" + v + "\" is not a place or a status"); return; }
      out.push(n != null ? { entrant: m[1].trim(), status: "Placed", place: n } : { entrant: m[1].trim(), status: st });
    });
    return { rows: out, errors: bad };
  }
  function openResults(id) {
    A.resEvent = id;
    var ev = A.data.events.filter(function (x) { return x.id === id; })[0], box = $("abResults");
    box.hidden = false;
    box.innerHTML = "<b>Results: " + esc(ev.name) + '</b><div class="note" style="margin:0">One line per entrant: <code>Name: 1</code> for a place (tied entrants repeat the place), or <code>Name: Missed cut</code>, <code>Withdrew</code>, <code>Did not start</code>, <code>Not classified</code>. Names must match the field exactly. You can save again to fix a result.</div><textarea id="abText" rows="10" style="padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);font:inherit;color:inherit"></textarea><div class="actions"><button class="btn primary" id="abSave" type="button">Save results</button><button class="btn" id="abClose" type="button">Close</button></div>';
  }
  function saveResults() {
    var p = parseResults($("abText").value);
    if (p.errors.length) { amsg(p.errors.slice(0, 3).join(" "), true); return; }
    if (!p.rows.length) { amsg("Enter at least one result.", true); return; }
    var id = A.resEvent;
    adminDo(function () { return rpc("enter_bonus_results", { p_event: id, p_results: p.rows }); }, p.rows.length + " results saved.", function () { $("abResults").hidden = true; });
  }

  // ---- standings, games, Cup and playoffs --------------------------------------------------------------------------
  var ST = { data: null, week: null };
  var KIND = { regular: "Regular season", "cup-group": "Cup group", "cup-semi": "Cup semifinal", "cup-final": "Cup final", wildcard: "Wildcard", "playoff-semi": "Semifinal", "playoff-final": "Final" };
  function loadStandings() {
    var seq = ST.seq = (ST.seq || 0) + 1;
    return rpc("screen_standings", ST.week ? { p_week: ST.week } : {}).then(function (d) { if (seq !== ST.seq) return ST.data || d; ST.data = d; ST.week = d.shown; S.offset = new Date(d.now).getTime() - Date.now(); return d; });
  }
  function num(v) { return v == null ? "–" : Number(v).toFixed(1); }
  function gameRow(g, me) {
    var a = g.name_a, b = g.name_b, sa = g.score_a, sb = g.score_b, done = g.winner != null;
    var hasScore = sa != null || sb != null;
    return '<li class="game"><span' + (done && g.winner === g.team_a ? ' class="win"' : "") + ">" + esc(a) + (g.team_a === me ? " (you)" : "") + '</span><span class="sc">' + (hasScore ? num(sa) + " – " + num(sb) : "vs") + (hasScore && !g.final && !done ? '<div class="note" style="margin:0">Live</div>' : "") + '</span><span style="text-align:right"' + (done && g.winner === g.team_b ? ' class="win"' : "") + ">" + (g.team_b === me ? "(you) " : "") + esc(b) + "</span></li>";
  }
  function renderStandings() {
    var d = ST.data, me = d.me;
    $("stNote").textContent = "Week " + d.week;
    $("stTable").innerHTML = "<thead><tr><th class='n'>#</th><th>Team</th><th class='n'>W</th><th class='n'>L</th><th class='n'>Points</th><th>Playoffs</th></tr></thead><tbody>" + d.standings.map(function (s, i) {
      var cut = s.seed === 4 || s.seed === 6;
      return "<tr class='" + (s.team_id === me ? "me" : "") + (cut ? " cut" : "") + "'><td class='n'>" + s.rank + "</td><td>" + esc(s.name) + (s.cup_bonus ? ' <span class="chip ok" title="Cup champion bonus win">Cup +1</span>' : "") + "</td><td class='n'>" + s.wins + "</td><td class='n'>" + s.losses + "</td><td class='n'>" + Number(s.points).toFixed(1) + "</td><td>" + (s.seed ? "Seed " + s.seed : "") + "</td></tr>";
    }).join("") + "</tbody>";
    $("gWeek").textContent = "Week " + d.shown;
    $("gPrev").disabled = d.shown <= 1; $("gNext").disabled = d.shown >= 51;
    $("gList").innerHTML = d.games.length ? d.games.map(function (g) { return gameRow(g, me) + ""; }).join("") : '<li class="note">No games scheduled this week.</li>';
    if (d.games.length) $("gList").insertAdjacentHTML("afterbegin", '<li class="eyebrow">' + esc(KIND[d.games[0].kind] || d.games[0].kind) + "</li>");
    $("mineList").innerHTML = !me ? '<li class="note">You do not own a team.</li>' : d.mine.length ? d.mine.map(function (g) {
      var res = g.winner == null ? (g.final ? '<span class="chip">Tie</span>' : g.score != null ? '<span class="chip warn">Live</span>' : '<span class="chip">Upcoming</span>') : g.winner === me ? '<span class="chip ok">W</span>' : '<span class="chip bad">L</span>';
      return '<li class="row"><span class="who"><span class="nm">Week ' + g.week + " vs " + esc(g.opp_name) + '</span><span class="meta"><span>' + esc(KIND[g.kind] || g.kind) + "</span>" + (g.score != null || g.opp_score != null ? "<span>" + num(g.score) + " – " + num(g.opp_score) + "</span>" : "") + '</span></span><span class="acts">' + res + "</span></li>";
    }).join("") : '<li class="note">The schedule has not been drawn yet.</li>';
    var byWeek = {};
    d.playoffs.forEach(function (g) { (byWeek[g.week] = byWeek[g.week] || []).push(g); });
    $("poList").innerHTML = d.playoffs.length ? Object.keys(byWeek).map(function (w) {
      return '<li class="eyebrow" style="margin-top:6px">Week ' + w + " · " + esc(KIND[byWeek[w][0].kind]) + "</li>" + byWeek[w].map(function (g) { return gameRow(g, me); }).join("");
    }).join("") : '<li class="note">The bracket is set after week 44 is final.</li>';
    var cup = d.cup;
    $("cupBody").innerHTML = cup.groups.length ? cup.groups.map(function (g) {
      return '<div class="eyebrow" style="margin-top:8px">Tier ' + g.tier + " · Group " + g.grp + "</div><table class='tbl'><tbody>" + g.teams.map(function (t) {
        return "<tr class='" + (t.id === me ? "me" : "") + "'><td>" + esc(t.name) + "</td><td class='n'>" + t.wins + "-" + t.losses + "</td><td class='n'>" + Number(t.points).toFixed(1) + "</td></tr>";
      }).join("") + "</tbody></table>";
    }).join("") + (cup.knockout.length ? '<div class="eyebrow" style="margin-top:12px">Knockout</div>' + cup.knockout.map(function (g) { return gameRow({ name_a: g.name_a, name_b: g.name_b, score_a: g.score_a, score_b: g.score_b, winner: g.winner, team_a: g.team_a, team_b: g.team_b, final: true }, me); }).join("") : "") : '<p class="note" style="margin:0">Cup groups are drawn when the Cup starts in week 20.</p>';
  }
  function stepWeek(n) { ST.week = Math.min(51, Math.max(1, ST.week + n)); loadStandings().then(renderStandings).catch(function (x) { $("stmsg").textContent = x.message; $("stmsg").className = "msg err"; }); }
  $("gPrev").addEventListener("click", function () { stepWeek(-1); });
  $("gNext").addEventListener("click", function () { stepWeek(1); });


  // ---- draft room ------------------------------------------------------------------------------------------------------
  var D = { data: null, sport: "", search: "", id: null };
  var dmsg = function (t, err) { $("dmsg").textContent = t || ""; $("dmsg").className = "msg" + (err ? " err" : ""); };
  function loadDraft() {
    return rpc("screen_draft", { p_draft: D.id, p_sport: D.sport || null, p_search: D.search || null, p_limit: 60 }).then(function (d) { D.data = d; S.offset = new Date(d.now).getTime() - Date.now(); return d; });
  }
  function teamName(id) { var t = (D.data.teams || []).filter(function (x) { return x.id === id; })[0]; return t ? t.name : "Team " + id; }
  function clockText(ms) { ms = Math.max(0, ms); var m = Math.floor(ms / 60000), s = Math.floor(ms % 60000 / 1000); return m + ":" + (s < 10 ? "0" : "") + s; }
  function myTurn() { var d = D.data; return d && d.draft && d.draft.status === "live" && d.me && d.on_clock === d.me; }
  function renderDraftClock() {
    var d = D.data; if (!d || d.none || d.draft.status !== "live" || !$("dClock")) return;
    var left = new Date(d.draft.deadline).getTime() - (Date.now() + S.offset);
    $("dClock").textContent = clockText(left);
    if (left <= 0 && !D.refreshing) { D.refreshing = true; loadDraft().then(renderDraft).catch(function () {}).then(function () { D.refreshing = false; }); }
  }
  function renderDraft() {
    var d = D.data;
    var many = d.drafts && d.drafts.length > 1;
    $("dPickField").hidden = !many;
    if (many) { $("dSelect").innerHTML = d.drafts.map(function (x) { return '<option value="' + x.id + '">' + esc(x.label) + " (" + x.status + ")</option>"; }).join(""); if (d.draft) $("dSelect").value = String(d.draft.id); }
    if (d.none) {
      $("dBanner").className = "lock"; $("dBanner").innerHTML = "<b>No draft yet</b><div class='note'>The commissioner creates the draft, then starts it. You will see it here.</div>";
      ["dAvail", "dQueue", "dRecent", "dMine"].forEach(function (id) { $(id).innerHTML = ""; }); $("dBoard").innerHTML = ""; return;
    }
    var dr = d.draft, n = dr.teams, round = Math.ceil(dr.current_pick / n), live = dr.status === "live";
    if (dr.status === "scheduled") $("dBanner").innerHTML = '<div class="eyebrow">' + (dr.kind === "main" ? "Main draft" : "Bonus draft") + '</div><b>Waiting for the commissioner to start</b><div class="note">' + dr.rounds + " rounds, " + dr.pick_seconds + " seconds per pick. Snake order: the order reverses every round.</div>";
    else if (dr.status === "done") $("dBanner").innerHTML = '<div class="eyebrow">Draft complete</div><b>All ' + dr.total + " picks are made</b>" + (dr.kind === "main" ? '<div class="note">Week-1 lineups are open. Set yours on the Lineup tab.</div>' : "");
    else $("dBanner").innerHTML = '<div class="eyebrow">Round ' + round + " · Pick " + dr.current_pick + " of " + dr.total + "</div><b>" + (myTurn() ? "You are on the clock" : esc(teamName(d.on_clock)) + " is on the clock") + '</b> <span class="chip ' + (myTurn() ? "ok" : "warn") + '" id="dClock"></span>' + (d.me ? '<div class="note">' + (myTurn() ? "Choose a player below." : nextPickText(d)) + "</div>" : "");
    $("dBanner").className = "lock" + (myTurn() ? " ok" : "");
    renderDraftClock();
    var queueIds = d.queue.map(function (q) { return q.id; });
    $("dCount").textContent = d.available.length + " shown";
    $("dAvail").innerHTML = d.available.length ? d.available.map(function (p) {
      var can = myTurn() && !p.problem, inq = queueIds.indexOf(p.id) >= 0;
      return '<li class="row"><span class="who"><span class="nm">' + esc(p.name) + '</span><span class="meta">' + (p.sport ? tag(p.sport) + "<span>" + esc(p.position) + "</span>" : "") + (p.rank != null ? "<span>Rank " + p.rank + "</span>" : "") + (p.problem && myTurn() ? '<span class="chip bad">' + esc(p.problem) + "</span>" : "") + '</span></span><span class="acts">' +
        (d.me && dr.status !== "done" ? '<button class="btn" data-dq="' + p.id + '"' + (inq ? " disabled" : "") + ">" + (inq ? "Queued" : "Queue") + "</button>" : "") +
        (can ? '<button class="btn primary" data-dpick="' + p.id + '">Draft</button>' : "") + "</span></li>";
    }).join("") : '<li class="row empty"><span>No players match</span><span></span></li>';
    $("dQueue").innerHTML = !d.me ? '<li class="note">You do not own a team.</li>' : d.queue.length ? d.queue.map(function (q, i) {
      return '<li class="row"><span class="who"><span class="nm">' + (i + 1) + ". " + esc(q.name) + '</span><span class="meta">' + (q.sport ? tag(q.sport) + "<span>" + esc(q.position) + "</span>" : "") + (q.taken ? '<span class="chip bad">Taken</span>' : "") + '</span></span><span class="acts"><button class="btn" data-qup="' + i + '" aria-label="Move up"' + (i === 0 ? " disabled" : "") + '>↑</button><button class="btn" data-qdown="' + i + '" aria-label="Move down"' + (i === d.queue.length - 1 ? " disabled" : "") + '>↓</button><button class="btn" data-qdel="' + i + '">Remove</button></span></li>';
    }).join("") : '<li class="note">Your queue is empty. Add players with Queue.</li>';
    var recent = d.picks.slice(-8).reverse();
    $("dRecent").innerHTML = recent.length ? recent.map(function (p) {
      return '<li class="row"><span class="who"><span class="nm">#' + p.pick_no + " " + esc(p.name) + '</span><span class="meta"><span>' + esc(teamName(p.team_id)) + "</span>" + (p.sport ? tag(p.sport) : "") + (p.auto ? '<span class="chip">Auto</span>' : "") + "</span></span><span></span></li>";
    }).join("") : '<li class="note">No picks yet.</li>';
    var mine = d.picks.filter(function (p) { return p.team_id === d.me; });
    $("dMine").innerHTML = mine.length ? mine.map(function (p) { return '<li class="row"><span class="who"><span class="nm">' + esc(p.name) + '</span><span class="meta"><span>Pick ' + p.pick_no + "</span>" + (p.sport ? tag(p.sport) + "<span>" + esc(p.position) + "</span>" : "") + "</span></span><span></span></li>"; }).join("") : '<li class="note">Nothing yet.</li>';
    renderBoard(d, n);
  }
  function nextPickText(d) {
    var n = d.draft.teams, next = d.slots.filter(function (s) { return s[1] === d.me && s[0] > d.draft.current_pick; })[0];
    return next ? "Your next pick is #" + next[0] + " (round " + Math.ceil(next[0] / n) + "), " + (next[0] - d.draft.current_pick) + " picks away." : "You have no picks left.";
  }
  function renderBoard(d, n) {
    var cols = d.slots.slice(0, n).map(function (s) { return s[1]; }), byPick = {}, cell = {};
    d.picks.forEach(function (p) { byPick[p.pick_no] = p; });
    d.slots.forEach(function (s) { cell[Math.ceil(s[0] / n) + ":" + s[1]] = s[0]; });
    var h = "<thead><tr><th class='n'>Rd</th>" + cols.map(function (c) { return "<th>" + esc(teamName(c).replace("Team ", "T")) + "</th>"; }).join("") + "</tr></thead><tbody>";
    for (var r = 1; r <= d.draft.rounds; r++) {
      h += "<tr><td class='n'>" + r + "</td>" + cols.map(function (c) {
        var no = cell[r + ":" + c], p = byPick[no], now = d.draft.status === "live" && no === d.draft.current_pick;
        return "<td style='font-size:12px;white-space:nowrap;" + (now ? "background:var(--accent-soft);font-weight:700" : "") + (c === d.me ? ";border-left:2px solid var(--accent)" : "") + "' title='Pick " + no + "'>" + (p ? esc(p.name) : now ? "On the clock" : "") + "</td>";
      }).join("") + "</tr>";
    }
    $("dBoard").innerHTML = h + "</tbody>";
  }
  function draftDo(call, okText) {
    if (S.busy) return; S.busy = true;
    call().then(function () { return loadDraft().then(function () { renderDraft(); dmsg(okText); }); })
      .catch(function (e) { return loadDraft().then(function () { renderDraft(); dmsg(e.message, true); }); })
      .then(function () { S.busy = false; });
  }
  function setQueue(ids, okText) { draftDo(function () { return rpc("queue_set", { p_draft: D.data.draft.id, p_items: ids }); }, okText || ""); }
  $("view-draft").addEventListener("click", function (e) {
    var b, q = D.data && D.data.queue ? D.data.queue.map(function (x) { return x.id; }) : [];
    if ((b = e.target.closest("[data-dpick]"))) { var id = +b.dataset.dpick, nm = D.data.available.filter(function (p) { return p.id === id; })[0].name; draftDo(function () { return rpc("make_pick", { p_draft: D.data.draft.id, p_item: id }); }, "You drafted " + nm + "."); return; }
    if ((b = e.target.closest("[data-dq]"))) { q.push(+b.dataset.dq); setQueue(q, "Added to your queue."); return; }
    if ((b = e.target.closest("[data-qdel]"))) { q.splice(+b.dataset.qdel, 1); setQueue(q, "Removed from your queue."); return; }
    if ((b = e.target.closest("[data-qup],[data-qdown]"))) { var i = +(b.dataset.qup || b.dataset.qdown), j = b.dataset.qup ? i - 1 : i + 1; q.splice(j, 0, q.splice(i, 1)[0]); setQueue(q, "Queue reordered."); }
  });
  $("dSelect").addEventListener("change", function (e) { D.id = +e.target.value; loadDraft().then(renderDraft).catch(function (x) { dmsg(x.message, true); }); });
  $("dSport").addEventListener("change", function (e) { D.sport = e.target.value; loadDraft().then(renderDraft).catch(function (x) { dmsg(x.message, true); }); });
  var dTimer;
  $("dSearch").addEventListener("input", function (e) { clearTimeout(dTimer); dTimer = setTimeout(function () { D.search = e.target.value.trim(); loadDraft().then(renderDraft).catch(function (x) { dmsg(x.message, true); }); }, 250); });
  setInterval(renderDraftClock, 1000);
  setInterval(function () { if (S.tab === "draft" && !S.busy && !document.hidden && D.data && !D.data.none && D.data.draft.status !== "done") loadDraft().then(renderDraft).catch(function () {}); }, 3000);

  // ---- change password (Supabase Auth: PUT /auth/v1/user) ------------------------------------------------------------
  $("pwOpen").addEventListener("click", function () { $("pwForm").hidden = !$("pwForm").hidden; $("pwMsg").textContent = ""; if (!$("pwForm").hidden) $("pwNew").focus(); });
  $("pwCancel").addEventListener("click", function () { $("pwForm").hidden = true; $("pwForm").reset(); $("pwMsg").textContent = ""; });
  $("pwForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var m = $("pwMsg"), a = $("pwNew").value, b = $("pwAgain").value;
    var fail = function (t) { m.textContent = t; m.className = "msg err"; };
    if (a.length < 8) return fail("Use at least 8 characters.");
    if (a !== b) return fail("The two passwords do not match.");
    fresh().then(function (t) { return fetch(CFG.url + "/auth/v1/user", { method: "PUT", headers: headers(t), body: JSON.stringify({ password: a }) }); })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.msg || j.error_description || j.message || "Could not change the password"); }); })
      .then(function () { $("pwForm").reset(); m.textContent = "Password changed."; m.className = "msg"; })
      .catch(function (x) { fail(x.message); });
  });

  $("signOut").addEventListener("click", function () { saveSession(null); S.data = null; show("login"); });
  $("login").addEventListener("submit", function (e) {
    e.preventDefault(); $("loginMsg").textContent = "";
    token("password", { email: $("email").value.trim(), password: $("password").value }).then(function (s) { saveSession(s); start(); })
      .catch(function (x) { $("loginMsg").textContent = x.message; $("loginMsg").className = "msg err"; });
  });

  function show(which) { $("boot").hidden = true; $("app").hidden = which !== "app"; $("login").hidden = which !== "login"; }
  function start() {
    S.team = null; S.week = null;
    load().then(function () {
      var d = S.data;
      $("teamSel").innerHTML = d.teams.map(function (t) { return '<option value="' + t.id + '">' + esc(t.name) + (t.id === d.me ? " (you)" : "") + "</option>"; }).join("");
      $("teamSel").value = String(d.team.id); TR.teamList = d.teams;
      $("weekSel").innerHTML = Array.from({ length: 51 }, function (_, i) { return "<option value='" + (i + 1) + "'>Week " + (i + 1) + "</option>"; }).join("");
      return loadMatch();
    }).then(function () { return rpc("screen_admin"); }).then(function (a) {
      S.admin = !!a.commissioner;
      $("tab-admin").hidden = !S.admin; $("tab-players").hidden = !S.data.me; $("tab-trades").hidden = !S.data.me;
      if (!S.data.me) $("tab-matchup").hidden = true;
      showTab(S.data.me ? "lineup" : "activity");
    }).then(function () { show("app"); render(); $("userLine").textContent = ""; loadHealth(); })
      .catch(function (e) { saveSession(null); show("login"); $("loginMsg").textContent = e.message; $("loginMsg").className = "msg err"; });
  }
  setInterval(function () { if (S.data && !S.busy && !S.sel && !S.mull && !document.hidden) load(true).then(function () { renderLineup(); if (S.tab === "matchup") return loadMatch().then(renderMatch); }).then(loadHealth).catch(function () { $("liveBadge").textContent = "Offline"; }); }, 30000);
  window.__app = S;
  loadSession();
  if (session) start(); else show("login");
  var installEvt = null, standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone;
  window.addEventListener("beforeinstallprompt", function (e) { e.preventDefault(); installEvt = e; if (!standalone) $("installBtn").hidden = false; });
  window.addEventListener("appinstalled", function () { installEvt = null; $("installBtn").hidden = true; });
  if (!standalone && /iphone|ipad|ipod/i.test(navigator.userAgent)) $("installBtn").hidden = false;
  $("installBtn").addEventListener("click", function () {
    if (installEvt) { installEvt.prompt(); installEvt.userChoice.then(function () { installEvt = null; $("installBtn").hidden = true; }); }
    else $("installHelp").hidden = !$("installHelp").hidden;
  });
  if ("serviceWorker" in navigator && location.protocol !== "file:") window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
})();
