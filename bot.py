import os, io, re, asyncio, uuid, socket, ipaddress, traceback
from urllib.parse import urlparse
from decimal import Decimal
from datetime import datetime
import asyncpg
import uvicorn
from fastapi import FastAPI, Request
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

DB_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRYPTO_KEY = os.getenv("CRYPTOMUS_API_KEY")
MERCHANT_ID = os.getenv("CRYPTOMUS_MERCHANT_ID")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS","").replace(" ","").split(",") if x}
ALLOWED_DOMAINS = {d.lower() for d in os.getenv("ALLOWED_DOMAINS","").replace(" ","").split(",") if d}

def check_link(url):
    # Users send their own gift link; refuse anything that could reach the server's internal network
    u=urlparse(url)
    if u.scheme not in ("http","https") or not u.hostname: raise ValueError("Link must start with http:// or https://")
    host=u.hostname.lower()
    if ALLOWED_DOMAINS and not any(host==d or host.endswith("."+d) for d in ALLOWED_DOMAINS): raise ValueError("This link's website is not supported")
    try: addrs={a[4][0] for a in socket.getaddrinfo(host,None)}
    except socket.gaierror: raise ValueError("Link website not found")
    for a in addrs:
        ip=ipaddress.ip_address(a)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified: raise ValueError("Link not allowed")
    return url

async def get_db():
    return await asyncpg.create_pool(DB_URL, min_size=2, max_size=5)

def calc_cost(v):
    if v > 10: raise ValueError("Max $10")
    return Decimal("1") if v <= 5 else Decimal("2")

def is_admin(uid):
    return uid in ADMIN_IDS

async def notify_admins(bot,text):
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid,text)
        except Exception: pass

async def notify_admins_photo(bot,data,caption):
    for aid in ADMIN_IDS:
        try: await bot.send_photo(aid,io.BytesIO(data),caption=caption[:1000])
        except Exception: pass

async def is_banned(uid):
    async with db.acquire() as conn:
        return bool(await conn.fetchval("SELECT banned FROM users WHERE telegram_id=$1",uid))

def max_redeem(pts):
    return Decimal("0") if pts <= 0 else Decimal("5") if pts < 2 else Decimal("10")

# ---------------- Country selection helpers ----------------

COUNTRIES=["india","united states","united kingdom","canada","australia","germany","france","singapore",
           "netherlands","spain","italy","brazil","mexico","philippines","pakistan","bangladesh","nigeria"]

PAYPAL_RE=re.compile(r"PayPal\s+International",re.I)

# Finds the element that currently shows the selected country (button text, input value,
# <select> label, flag alt text, aria-label) and marks its clickable ancestor.
DETECT_COUNTRY_JS=r"""
(names) => {
  document.querySelectorAll('[data-hermes-country]').forEach(e=>e.removeAttribute('data-hermes-country'));
  const vis=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
  const clean=t=>(t||'').replace(/[^\p{L}\s]/gu,' ').replace(/\s+/g,' ').trim().toLowerCase();
  const INTER='button,select,input,a,label,[role=combobox],[role=button],[role=listbox],[aria-haspopup],[aria-expanded],[tabindex]';
  const cands=[];
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue;
    const tag=e.tagName;
    let t='';
    if (tag==='SELECT') t=e.selectedOptions&&e.selectedOptions[0]?e.selectedOptions[0].textContent:'';
    else if (tag==='INPUT') t=e.value||'';
    else if (tag==='IMG') t=e.alt||'';
    else { const it=e.innerText||''; if (it.length<=40) t=it; }
    const labels=[t,e.getAttribute('aria-label')||'',e.getAttribute('title')||''];
    for (const raw of labels) {
      const c=clean(raw);
      if (!c) continue;
      const name=names.find(n=>c===n||(c.includes(n)&&c.length<=n.length+20));
      if (!name) continue;
      const target=(tag==='SELECT'||tag==='INPUT')?e:(e.closest(INTER)||e);
      const hint=clean((target.getAttribute('class')||'')+' '+(target.getAttribute('aria-label')||'')+' '+(target.getAttribute('name')||'')+' '+(target.id||''));
      let score=0;
      if (target!==e||['SELECT','INPUT','BUTTON'].includes(tag)) score-=10;
      if (/country|region|locale/.test(hint)) score-=20;
      score+=c.length;
      cands.push({target,name,score,tag:target.tagName});
      break;
    }
  }
  if (!cands.length) return null;
  cands.sort((a,b)=>a.score-b.score);
  cands[0].target.setAttribute('data-hermes-country','1');
  return {name:cands[0].name,tag:cands[0].tag};
}
"""

# Finds the "India" option inside the opened picker (not the trigger itself).
FIND_INDIA_OPTION_JS=r"""
() => {
  document.querySelectorAll('[data-hermes-india]').forEach(e=>e.removeAttribute('data-hermes-india'));
  const vis=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
  const clean=t=>(t||'').replace(/[^\p{L}\s]/gu,' ').replace(/\s+/g,' ').trim().toLowerCase();
  const OPT='[role=option],li,[role=menuitem],[role=menuitemradio]';
  const trig=document.querySelector('[data-hermes-country]');
  const cands=[];
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)||e.tagName==='INPUT'||e.tagName==='SELECT') continue;
    // Skip the trigger itself and its ancestors, but NOT its descendants:
    // the dropdown menu is often rendered inside the same control.
    if (trig&&e.contains(trig)) continue;
    if (/singlevalue|single-value|placeholder/i.test(e.className||'')) continue;
    const c=clean(e.innerText);
    if (c==='india') cands.push(e);
  }
  if (!cands.length) return false;
  const score=e=>(e.closest(OPT)?0:1);
  cands.sort((a,b)=>score(a)-score(b)||(a.innerText.length-b.innerText.length));
  const t=cands[0].closest(OPT)||cands[0];
  t.setAttribute('data-hermes-india','1');
  return true;
}
"""

# Diagnostic: what controls are visible on the page right now.
DEBUG_CONTROLS_JS=r"""
() => {
  const vis=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
  const out=[];
  for (const e of document.querySelectorAll('button,select,input,[role=combobox],[role=button],[aria-haspopup]')) {
    if (!vis(e)) continue;
    let t=(e.tagName==='INPUT'?(e.value||e.placeholder||''):(e.innerText||e.getAttribute('aria-label')||'')).replace(/\s+/g,' ').trim().slice(0,40);
    out.push(e.tagName.toLowerCase()+(e.getAttribute('role')?'['+e.getAttribute('role')+']':'')+':'+(t||'-'));
    if (out.length>=20) break;
  }
  return out.join(' | ');
}
"""

async def paypal_visible(page):
    try:
        loc=page.get_by_text(PAYPAL_RE)
        for i in range(await loc.count()):
            if await loc.nth(i).is_visible(): return True
    except Exception: pass
    return False

async def debug_controls(page):
    try: return await page.evaluate(DEBUG_CONTROLS_JS)
    except Exception as e: return f"(debug failed: {e})"

async def click_first_visible(locators):
    for loc in locators:
        try:
            for i in range(min(await loc.count(),5)):
                l=loc.nth(i)
                if await l.is_visible():
                    await l.click(timeout=5000)
                    return True
        except Exception:
            continue
    return False

async def ensure_india(page):
    """Returns a status string. Raises Exception with diagnostics on failure."""
    # 1) Wait up to 30s for either a country control or PayPal International to render.
    detected=None
    for _ in range(30):
        try: detected=await page.evaluate(DETECT_COUNTRY_JS,COUNTRIES)
        except Exception: detected=None
        if detected or await paypal_visible(page): break
        await page.wait_for_timeout(1000)

    shown=detected["name"].title() if detected else "not detected"
    status=f"Country shown before selection: {shown}"

    # 2) Already India -> nothing to do.
    if detected and detected["name"]=="india":
        return status+" (already India)"

    # 3) Country unknown but PayPal International already available -> skip the picker.
    if not detected and await paypal_visible(page):
        return status+" (PayPal International already available, picker skipped)"

    # 4) Open the picker.
    opened=False
    if detected:
        trig=page.locator("[data-hermes-country]").first
        if detected["tag"]=="SELECT":
            try:
                await trig.select_option(label="India")
                await page.wait_for_timeout(1500)
                return status+" -> India (native select)"
            except Exception:
                pass
        try:
            await trig.click(timeout=5000)
            opened=True
        except Exception:
            pass

    if not opened:
        opened=await click_first_visible([
            page.get_by_label(re.compile(r"country|region",re.I)),
            page.get_by_role("combobox"),
            page.get_by_role("button",name=re.compile(r"country|region",re.I)),
            page.get_by_text(re.compile(r"^\s*change( country| region)?\s*$",re.I)),
            page.locator('[aria-haspopup="listbox"]'),
            page.locator('[class*="country" i]'),
            page.locator('[class*="region" i]'),
            page.locator('[data-testid*="country" i]'),
        ])

    if not opened:
        if await paypal_visible(page):
            return status+" (no picker found, PayPal International visible)"
        raise Exception(f"Could not open country selector. {status}. Visible controls: {await debug_controls(page)}")

    await page.wait_for_timeout(800)

    # Native <select> revealed after click?
    try:
        sel=page.locator("select")
        for i in range(await sel.count()):
            s=sel.nth(i)
            if await s.is_visible():
                try:
                    await s.select_option(label="India")
                    await page.wait_for_timeout(1500)
                    return status+" -> India (select)"
                except Exception:
                    pass
    except Exception:
        pass

    # 5) Type into the picker's search box (or directly via keyboard if none).
    typed=False
    for loc in [
        page.locator('[role="dialog"] input, [role="listbox"] input, [role="combobox"] input, input[role="combobox"]'),
        page.locator('input[placeholder*="country" i], input[placeholder*="search" i], input[aria-label*="country" i], input[aria-label*="search" i]'),
        page.locator('[data-hermes-country] input, input[data-hermes-country]'),
        page.locator('input[type="text"], input[type="search"], input:not([type])'),
    ]:
        try:
            for i in range(await loc.count()):
                box=loc.nth(i)
                if await box.is_visible() and await box.is_editable():
                    await box.fill("")
                    await box.type("India",delay=60)
                    typed=True
                    break
        except Exception:
            continue
        if typed: break
    if not typed:
        try:
            await page.keyboard.type("India",delay=60)
            typed=True
        except Exception:
            pass

    await page.wait_for_timeout(1000)

    # 6) Click the exact "India" option. Never press Enter: typing "india" lists
    #    "British Indian Ocean Territory" FIRST, so Enter would pick the wrong country.
    picked=False
    for _ in range(6):
        picked=await click_first_visible([
            page.get_by_role("option",name=re.compile(r"^\W*India\W*$",re.I)),
            page.get_by_role("menuitem",name=re.compile(r"^\W*India\W*$",re.I)),
        ])
        if picked: break
        try:
            if await page.evaluate(FIND_INDIA_OPTION_JS):
                await page.locator("[data-hermes-india]").first.click(timeout=5000)
                picked=True
                break
        except Exception:
            pass
        await page.wait_for_timeout(800)

    if not picked:
        raise Exception(f"India option not found in the country list. {status}. Visible controls: {await debug_controls(page)}")

    # 7) Verify: India shown AND PayPal International available (that's what we need).
    for _ in range(15):
        if await paypal_visible(page): break
        await page.wait_for_timeout(1000)
    try: after=await page.evaluate(DETECT_COUNTRY_JS,COUNTRIES)
    except Exception: after=None
    now=after["name"] if after else "unknown"
    if await paypal_visible(page):
        return status+f" -> {now.title()}"

    raise Exception(f"Country changed but PayPal International did not appear. {status}. Now shown: {now}. Visible controls: {await debug_controls(page)}")

# ---------------- Browser ----------------

class Browser:
    def __init__(self):
        self.pw = self.br = self.ctx = None
        self.sid = str(uuid.uuid4())[:8]
    async def launch(self):
        self.pw = await async_playwright().start()
        self.br = await self.pw.chromium.launch(headless=True,args=["--no-sandbox","--disable-setuid-sandbox"])
        self.ctx = await self.br.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",viewport={"width":1920,"height":1080},extra_http_headers={"DNT":"1","Sec-GPC":"1"})
        await self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});window.chrome={runtime:{}};")
        return await self.ctx.new_page()
    async def destroy(self):
        if self.ctx:
            for p in self.ctx.pages:
                try: await p.evaluate("localStorage.clear();sessionStorage.clear();document.cookie.split(';').forEach(c=>{document.cookie=c.replace(/^ +/,'').replace(/=.*/,'=;expires='+new Date().toUTCString()+';path=/')})")
                except Exception: pass
            await self.ctx.clear_cookies()
        if self.br: await self.br.close()
        if self.pw: await self.pw.stop()
        self.pw=self.br=self.ctx=None

async def redeem_card(db,uid,card_val,email,link):
    stage="starting"
    browser=None
    page=None
    browser_sid="unknown"
    shot=None
    country_status=""

    async with db.acquire() as conn:
        user=await conn.fetchrow("SELECT user_id,total_points FROM users WHERE telegram_id=$1",uid)
        if not user: raise ValueError("User not found")
        cost=calc_cost(card_val)
        if is_admin(uid): cost=Decimal("0")
        else:
            if user["total_points"]<cost: raise ValueError(f"Need {cost} pts, have {user['total_points']}")
            if card_val>max_redeem(user["total_points"]): raise ValueError(f"Max ${max_redeem(user['total_points'])} with your points")

        await conn.execute("UPDATE users SET total_points=total_points-$1 WHERE user_id=$2",cost,user["user_id"])

        browser=Browser()
        success=False
        err=None

        try:
            stage="launching browser"
            page=await browser.launch()
            browser_sid=getattr(browser,"sid","unknown")

            stage="opening gift-card link"
            await page.goto(link,wait_until="domcontentloaded",timeout=60000)
            stage="waiting for gift-card page to finish loading"
            try:
                await page.wait_for_load_state("networkidle",timeout=60000)
            except Exception:
                pass

            await asyncio.to_thread(check_link,page.url)

            try:
                await page.get_by_text("Select a product",exact=True).wait_for(state="visible",timeout=30000)
            except Exception:
                pass

            # --- Make sure India is selected ---
            stage="checking/selecting country (India)"
            try:
                country_status=await ensure_india(page)
                stage=f"checking/selecting country (India) | {country_status}"
            except Exception as e:
                raise Exception(f"Could not select India: {e}")

            # --- Select PayPal International ---
            stage="selecting PayPal International"
            try:
                paypal=page.get_by_text(PAYPAL_RE).last
                await paypal.wait_for(state="visible",timeout=60000)
                await paypal.click()
                await page.wait_for_timeout(1000)
            except Exception as e:
                raise Exception(f"Could not select PayPal International: {e} | {country_status}")

            # --- Wait for BOTH PayPal email fields ---
            stage="waiting for PayPal email fields"
            try:
                email_fields=page.locator(
                    'input[type="email"],'
                    'input[name*="email" i],'
                    'input[id*="email" i]'
                )
                await email_fields.first.wait_for(state="visible",timeout=60000)
                for _ in range(60):
                    if await email_fields.count() >= 2:
                        break
                    await page.wait_for_timeout(1000)
                if await email_fields.count() < 2:
                    raise Exception("Both PayPal email fields did not load")
                await email_fields.nth(0).fill(email)
                await email_fields.nth(1).fill(email)
            except Exception as e:
                raise Exception(f"Could not fill both PayPal email fields: {e}")

            # --- Click Transfer $X.XX USD ---
            stage=f"finding/clicking Transfer ${card_val:.2f} USD"
            try:
                transfer_button=page.get_by_role("button",name=f"Transfer ${card_val:.2f} USD")
                await transfer_button.wait_for(state="visible",timeout=60000)
                await transfer_button.click()
            except Exception:
                clicked=False
                for sel in [
                    'button:has-text("Transfer $")',
                    'button:has-text("Transfer")',
                    'button[type="submit"]'
                ]:
                    try:
                        button=page.locator(sel).first
                        await button.wait_for(state="visible",timeout=10000)
                        await button.click()
                        clicked=True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise Exception("Transfer button not found")

            try:
                await page.wait_for_load_state("networkidle",timeout=30000)
            except Exception:
                pass

            await page.wait_for_timeout(3000)

            txt=(await page.content()).lower()
            success=any(w in txt for w in ["success","confirmed","redeemed","completed","thank you","processed"])

        except Exception as e:
            exc_type=type(e).__name__
            exc_msg=str(e).strip() or repr(e)

            current_url="unknown"
            page_title="unknown"
            try:
                if page:
                    current_url=page.url
                    page_title=await page.title()
                    shot=await page.screenshot(full_page=True,timeout=15000)
            except Exception:
                pass

            tb_lines=traceback.format_exc().strip().splitlines()
            trace_tail=" | ".join(tb_lines[-8:])

            err=(
                f"{exc_type}: {exc_msg}\n"
                f"Stage: {stage}\n"
                f"URL: {current_url}\n"
                f"Page title: {page_title}\n"
                f"Trace: {trace_tail}"
            )
            if len(err)>3500:
                err=err[:2200]+"\n... [trace shortened] ...\n"+err[-1200:]

            await conn.execute("UPDATE users SET total_points=total_points+$1 WHERE user_id=$2",cost,user["user_id"])

        finally:
            try:
                if browser:
                    await browser.destroy()
            except Exception as destroy_error:
                if not err:
                    err=f"{type(destroy_error).__name__}: {destroy_error}\nStage: browser cleanup"

        rid=await conn.fetchval(
            "INSERT INTO redemptions"
            "(user_id,points_spent,paypal_email,status,browser_session_id,error_message,gift_url)"
            "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING redemption_id",
            user["user_id"],cost,email,"completed" if success else "failed",browser_sid,err,link
        )

        return {"success":success,"rid":rid,"cost":float(cost),"err":err,"shot":shot}

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO users(telegram_id)VALUES($1)ON CONFLICT DO NOTHING",uid)
    if await is_banned(uid): await update.message.reply_text("🚫 You are banned.");return
    if is_admin(uid): await update.message.reply_text("👑 Admin mode: redeems are free, Buy gives test points.\nType /admin for admin commands.")
    await update.message.reply_text("🤖 Hermes Redeem Bot\n\n💰 Buy Points ($10)\n💎 $1 = 2 Points\n🎁 1pt→$5 max | 2pts→$10 max",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Buy",callback_data="buy"),InlineKeyboardButton("🎁 Redeem",callback_data="redeem")],[InlineKeyboardButton("📊 Balance",callback_data="bal")]]))

async def callback(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer();uid=q.from_user.id;data=q.data
    if await is_banned(uid): await q.edit_message_text("🚫 You are banned.");return
    async with db.acquire() as conn:
        if data.startswith("ap_"):
            if not is_admin(uid): return
            tg_id=await confirm_payment(conn,data[3:])
            await q.edit_message_text(f"✅ Approved {data[3:]}" if tg_id else f"⚠️ {data[3:]} not pending")
            if tg_id:
                try: await ctx.bot.send_message(tg_id,"✅ Payment confirmed! 20 points added.")
                except Exception: pass
            return
        if data=="buy" and is_admin(uid):
            await conn.execute("INSERT INTO users(telegram_id)VALUES($1)ON CONFLICT DO NOTHING",uid)
            await conn.execute("UPDATE users SET total_points=total_points+20 WHERE telegram_id=$1",uid)
            await q.edit_message_text("👑 Admin test: 20 points added free (no payment).",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="back")]]))
        elif data=="buy":
            oid=f"H{uid}{int(datetime.now().timestamp())}"
            await conn.execute("INSERT INTO users(telegram_id)VALUES($1)ON CONFLICT DO NOTHING",uid)
            db_uid=await conn.fetchval("SELECT user_id FROM users WHERE telegram_id=$1",uid)
            await conn.execute("INSERT INTO transactions(user_id,crypto_tx_hash,amount_usd,points_earned)VALUES($1,$2,$3,$4)",db_uid,oid,10,20)
            await q.edit_message_text(f"💰 Pay $10\n🔗 Invoice: {oid}\n⏳ Click 'Paid' after payment",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Paid",callback_data=f"paid_{oid}")],[InlineKeyboardButton("🔙 Back",callback_data="back")]]))
        elif data.startswith("paid_"):
            oid=data.split("_",1)[1]
            tx=await conn.fetchrow("SELECT status FROM transactions WHERE crypto_tx_hash=$1",oid)
            if tx and tx["status"]=="confirmed": await q.edit_message_text("✅ Already credited! 20 points added.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="back")]]))
            else: await q.edit_message_text("⏳ Waiting for blockchain confirmation...\nCheck again in 2-3 minutes.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="back")]]))
        elif data=="redeem":
            await q.edit_message_text("🎁 Select card value:\n1pt: $3,$5 | 2pts: $6,$10\nMax $10 per redeem",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("$3",callback_data="r_3"),InlineKeyboardButton("$5",callback_data="r_5")],[InlineKeyboardButton("$6",callback_data="r_6"),InlineKeyboardButton("$10",callback_data="r_10")],[InlineKeyboardButton("🔙 Back",callback_data="back")]]))
        elif data.startswith("r_"):
            val=int(data.split("_")[1]);ctx.user_data["rv"]=val
            u=await conn.fetchrow("SELECT total_points FROM users WHERE telegram_id=$1",uid)
            pts=u["total_points"] if u else 0
            if val>max_redeem(pts) and not is_admin(uid): await q.edit_message_text(f"❌ Max ${max_redeem(pts)} with {pts} points",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="back")]]));return
            await q.edit_message_text(f"🎁 Redeeming ${val}\n🔗 Send your gift card link:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="back")]]))
            ctx.user_data["step"]="link"
        elif data=="bal":
            u=await conn.fetchrow("SELECT total_points FROM users WHERE telegram_id=$1",uid)
            pts=u["total_points"] if u else 0
            extra="\n👑 Admin: redeems are free" if is_admin(uid) else ""
            await q.edit_message_text(f"📊 Balance: {pts} points\n💵 Max redeem: ${max_redeem(pts)}{extra}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="back")]]))
        elif data=="back":
            ctx.user_data["step"]=None
            await q.edit_message_text("🤖 Hermes Redeem Bot\n\n💰 Buy Points ($10)\n💎 $1 = 2 Points\n🎁 1pt→$5 max | 2pts→$10 max",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Buy",callback_data="buy"),InlineKeyboardButton("🎁 Redeem",callback_data="redeem")],[InlineKeyboardButton("📊 Balance",callback_data="bal")]]))

async def msg(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    step=ctx.user_data.get("step")
    if not step: return
    if await is_banned(update.effective_user.id): return
    text=update.message.text.strip()
    if step=="link":
        try: ctx.user_data["link"]=await asyncio.to_thread(check_link,text)
        except ValueError as e: await update.message.reply_text(f"❌ {e}\n🔗 Send a valid gift card link:");return
        ctx.user_data["step"]="email"
        await update.message.reply_text("✅ Link saved\n📧 Now enter your PayPal email:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="back")]]))
        return
    email=text
    if "@" not in email or "." not in email.split("@")[1]: await update.message.reply_text("❌ Invalid email");return
    val=ctx.user_data.get("rv",0);link=ctx.user_data.get("link")
    ctx.user_data["step"]=None
    await update.message.reply_text("⏳ Processing...")
    try:
        res=await redeem_card(db,update.effective_user.id,val,email,link)
        if res["success"]: await update.message.reply_text(f"✅ ${val} redeemed!\n💎 Spent: {res['cost']} pts\n📧 {email}\n🔒 Browser wiped.")
        else: await update.message.reply_text(f"❌ Failed: {res['err']}\n💎 Points refunded.")
        await notify_admins(ctx.bot,f"🎁 Redeem #{res['rid']} {'✅ OK' if res['success'] else '❌ FAILED'}\nUser: {update.effective_user.id}\n${val} → {email}\n🔗 {link}\nCost: {res['cost']} pts"+(f"\nError: {res['err']}" if res['err'] else ""))
        if res.get("shot"):
            await notify_admins_photo(ctx.bot,res["shot"],f"📸 Redeem #{res['rid']} page at failure")
    except ValueError as e: await update.message.reply_text(f"❌ {e}")
    ctx.user_data["rv"]=ctx.user_data["link"]=None

web=FastAPI()

@web.get("/")
async def health():
    return {"status":"ok"}

@web.post("/webhook")
async def webhook(request:Request):
    data=await request.json()
    if data.get("status")=="paid" and data.get("order_id"):
        async with db.acquire() as conn:
            tg_id=await confirm_payment(conn,data["order_id"])
        if tg_id: await notify_admins(tg_app.bot,f"💰 Payment confirmed: {data['order_id']} (user {tg_id})")
    return {"status":"ok"}

async def confirm_payment(conn,oid):
    async with conn.transaction():
        tx=await conn.fetchrow("SELECT * FROM transactions WHERE crypto_tx_hash=$1 FOR UPDATE",oid)
        if not tx or tx["status"]!="pending": return None
        await conn.execute("UPDATE transactions SET status='confirmed' WHERE tx_id=$1",tx["tx_id"])
        return await conn.fetchval("UPDATE users SET total_points=total_points+$1 WHERE user_id=$2 RETURNING telegram_id",tx["points_earned"],tx["user_id"])

ADMIN_HELP=("👑 Admin commands\n\n"
    "/admin - stats\n"
    "/addpoints <tg_id> <amount>\n"
    "/removepoints <tg_id> <amount>\n"
    "/user <tg_id> - user info\n"
    "/pending - unpaid invoices (approve buttons)\n"
    "/redeems - last 10 redeems\n"
    "/broadcast <message>\n"
    "/ban <tg_id> | /unban <tg_id>\n\n"
    "Your own redeems are free and Buy gives 20 test points.")

def admin_only(fn):
    async def wrap(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id): return
        try: await fn(update,ctx)
        except (IndexError,ValueError,ArithmeticError): await update.message.reply_text("❌ Wrong format. See /admin")
    return wrap

@admin_only
async def admin_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    async with db.acquire() as conn:
        users=await conn.fetchval("SELECT COUNT(*) FROM users")
        pts=await conn.fetchval("SELECT COALESCE(SUM(total_points),0) FROM users")
        paid=await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE status='confirmed'")
        pend=await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE status='pending'")
        rok=await conn.fetchval("SELECT COUNT(*) FROM redemptions WHERE status='completed'")
        rbad=await conn.fetchval("SELECT COUNT(*) FROM redemptions WHERE status='failed'")
    await update.message.reply_text(f"📊 Stats\n👥 Users: {users}\n💎 Points held: {pts}\n💰 Payments: {paid} confirmed, {pend} pending\n🎁 Redeems: {rok} ok, {rbad} failed\n\n"+ADMIN_HELP)

async def change_points(update,ctx,sign):
    tg_id=int(ctx.args[0]);amt=Decimal(ctx.args[1])
    if amt<=0: raise ValueError
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO users(telegram_id)VALUES($1)ON CONFLICT DO NOTHING",tg_id)
        new=await conn.fetchval("UPDATE users SET total_points=GREATEST(total_points+$1,0) WHERE telegram_id=$2 RETURNING total_points",sign*amt,tg_id)
    await update.message.reply_text(f"✅ User {tg_id} now has {new} points")

@admin_only
async def addpoints_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await change_points(update,ctx,1)

@admin_only
async def removepoints_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await change_points(update,ctx,-1)

@admin_only
async def user_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    tg_id=int(ctx.args[0])
    async with db.acquire() as conn:
        u=await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1",tg_id)
        if not u: await update.message.reply_text("❌ User not found");return
        txs=await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE user_id=$1 AND status='confirmed'",u["user_id"])
        rds=await conn.fetch("SELECT redemption_id,points_spent,paypal_email,status FROM redemptions WHERE user_id=$1 ORDER BY redemption_id DESC LIMIT 5",u["user_id"])
    lines="\n".join(f"#{r['redemption_id']} {r['status']} {r['points_spent']}pts → {r['paypal_email']}" for r in rds) or "none"
    await update.message.reply_text(f"👤 {tg_id}{' 🚫 BANNED' if u['banned'] else ''}\n💎 Points: {u['total_points']}\n💰 Payments: {txs}\n📅 Joined: {u['created_at']:%Y-%m-%d}\n\n🎁 Last redeems:\n{lines}")

@admin_only
async def pending_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    async with db.acquire() as conn:
        rows=await conn.fetch("SELECT t.crypto_tx_hash,t.amount_usd,u.telegram_id,t.created_at FROM transactions t JOIN users u ON u.user_id=t.user_id WHERE t.status='pending' ORDER BY t.tx_id DESC LIMIT 10")
    if not rows: await update.message.reply_text("✅ No pending payments");return
    for r in rows:
        await update.message.reply_text(f"⏳ {r['crypto_tx_hash']}\n👤 {r['telegram_id']} | ${r['amount_usd']}\n📅 {r['created_at']:%Y-%m-%d %H:%M}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve",callback_data=f"ap_{r['crypto_tx_hash']}")]]))

@admin_only
async def redeems_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    async with db.acquire() as conn:
        rows=await conn.fetch("SELECT r.redemption_id,r.status,r.points_spent,r.paypal_email,r.error_message,r.gift_url,u.telegram_id FROM redemptions r JOIN users u ON u.user_id=r.user_id ORDER BY r.redemption_id DESC LIMIT 10")
    text="\n\n".join(f"#{r['redemption_id']} {'✅' if r['status']=='completed' else '❌'} {r['telegram_id']}\n{r['points_spent']}pts → {r['paypal_email']}\n🔗 {r['gift_url'] or '-'}"+(f"\n⚠️ {r['error_message'][:100]}" if r['error_message'] else "") for r in rows)
    await update.message.reply_text(text or "No redeems yet")

@admin_only
async def broadcast_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    text=update.message.text.partition(" ")[2].strip()
    if not text: raise ValueError
    async with db.acquire() as conn:
        ids=[r["telegram_id"] for r in await conn.fetch("SELECT telegram_id FROM users WHERE NOT banned")]
    sent=0
    for tg_id in ids:
        try: await ctx.bot.send_message(tg_id,f"📢 {text}");sent+=1
        except Exception: pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Sent to {sent}/{len(ids)} users")

async def set_ban(update,ctx,banned):
    tg_id=int(ctx.args[0])
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO users(telegram_id)VALUES($1)ON CONFLICT DO NOTHING",tg_id)
        await conn.execute("UPDATE users SET banned=$1 WHERE telegram_id=$2",banned,tg_id)
    await update.message.reply_text(f"{'🚫 Banned' if banned else '✅ Unbanned'} {tg_id}")

@admin_only
async def ban_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await set_ban(update,ctx,True)

@admin_only
async def unban_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await set_ban(update,ctx,False)

async def main():
    global db,tg_app
    db=await get_db()
    async with db.acquire() as conn:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"schema.sql"),encoding="utf-8") as f:
            await conn.execute(f.read())
    app=tg_app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    for name,fn in [("admin",admin_cmd),("addpoints",addpoints_cmd),("removepoints",removepoints_cmd),("user",user_cmd),("pending",pending_cmd),("redeems",redeems_cmd),("broadcast",broadcast_cmd),("ban",ban_cmd),("unban",unban_cmd)]:
        app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,msg))
    server=uvicorn.Server(uvicorn.Config(web,host="0.0.0.0",port=int(os.getenv("PORT","8000")),log_level="info"))
    async with app:
        await app.start()
        await app.updater.start_polling()
        print("[HERMES] Bot active")
        try:
            await server.serve()
        finally:
            await app.updater.stop()
            await app.stop()

if __name__=="__main__":
    asyncio.run(main())
