import os

with open('telebot.py', 'r', encoding='utf-8') as f:
    content = f.read()

start_marker = "async def run_oanda_backtest("
end_marker = "# --- LIVE ACCOUNT MANAGER (DD & REPORTS) ---"

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    new_func = """async def run_oanda_backtest(start_dt, end_dt=None):
    if bot_state['is_backtesting']:
        await send_tg_msg("⚠️ يوجد باك تيست قيد المعالجة حالياً.")
        return
        
    bot_state['is_backtesting'] = True
    c_log("بدء الباك تيست وتحليل النتائج...")
    csv_filename = f"BT_Report_{datetime.now().strftime('%H%M%S')}.csv"
    trade_logs = []
    
    sim_balance = 10000.0 
    total_gross_profit = 0.0
    total_gross_loss = 0.0
    peak_equity = sim_balance
    max_dd_pct = 0.0
    
    msg_dt = f"من: {start_dt.strftime('%Y-%m-%d')}" + (f" إلى: {end_dt.strftime('%Y-%m-%d')}" if end_dt else "")
    await send_tg_msg(f"⏳ <b>جاري إجراء الباك تيست...</b>\\n{msg_dt}\\n(يتم الفحص بدقة 1-Minute لكل صفقة، قد يستغرق بعض الوقت)")
    
    try:
        for tf in bot_state['timeframes']:
            if not bot_state['active_tfs'][tf]: continue
            
            c_data = await fetch_oanda_candles('XAU_USD', tf, 5000)
            if len(c_data) < 150: continue
            import pandas as pd
            df = calculate_indicators(pd.DataFrame(c_data).sort_values(by='time').reset_index(drop=True))
            
            mask = (df['time'] >= start_dt) & (df['time'] <= end_dt) if end_dt else (df['time'] >= start_dt)
            for i in df[mask].index:
                if i < max(3, bot_state['cons_count']): continue 
                
                curr, prev = df.loc[i], df.loc[i-1]
                if bot_state['use_time_filter'] and not (8 <= curr['time'].hour <= 17): continue
                
                b_ema, s_ema = True, True
                cons = bot_state['cons_count'] if bot_state['use_f_cons'] else 1
                for j in range(cons):
                    c = df.loc[i-j]
                    if not (c['ema50'] > c['ema150']): b_ema = False
                    if not (c['ema150'] > c['ema50']): s_ema = False

                buy_triggers = check_stoch_signal(prev['K'], curr['K'], 'buy')
                sell_triggers = check_stoch_signal(prev['K'], curr['K'], 'sell')
                
                entered_this_candle = False
                
                if buy_triggers:
                    for lvl in buy_triggers:
                        is_deep = (lvl == 10)
                        can_enter = (not bot_state['use_trend_filter']) or (bot_state['use_smart_filter'] and is_deep) or b_ema
                        ent_time_str = (curr['time'] + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
                        
                        if can_enter and not entered_this_candle:
                            entered_this_candle = True
                            act_ent = curr['close'] + (bot_state['spread_pips'] * bot_state['pip_value'])
                            tp_p = round(act_ent + (bot_state['tp_pips'][tf] * bot_state['pip_value']), 2)
                            sl_p = round(act_ent - (bot_state['sl_pips'][tf] * bot_state['pip_value']), 2)
                            eff_tp_p = tp_p - (bot_state['tp_tolerance_pips'] * bot_state['pip_value'])
                            
                            max_ext = min(curr['time'] + timedelta(hours=72), datetime.now(timezone.utc))
                            v_cands = await fetch_oanda_candles('XAU_USD', '1m', 4320, max_ext)
                            outcome, ext_t = "EXPIRED", max_ext
                            be_activated = False
                            be_target = act_ent + (20 * bot_state['pip_value'])
                            
                            for c in [v for v in v_cands if curr['time'] <= v['time'] <= max_ext]:
                                if bot_state['use_be'] and not be_activated and c['high'] >= be_target: sl_p = act_ent; be_activated = True
                                if c['low'] <= sl_p: outcome, ext_t = ("BREAK-EVEN" if be_activated and sl_p == act_ent else "LOSS"), c['time']; break
                                if c['high'] >= eff_tp_p: outcome, ext_t = "WIN", c['time']; break
                            
                            p_usd = 0.0
                            if outcome in ["WIN", "LOSS"]: 
                                p_usd = round(abs(act_ent - (tp_p if outcome=="WIN" else sl_p)) * 100 * bot_state['lot_size'], 2) * (1 if outcome=="WIN" else -1)
                            
                            if p_usd > 0: total_gross_profit += p_usd
                            elif p_usd < 0: total_gross_loss += abs(p_usd)
                            
                            current_equity = sim_balance + total_gross_profit - total_gross_loss
                            peak_equity = max(peak_equity, current_equity)
                            dd_pct = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0
                            max_dd_pct = max(max_dd_pct, dd_pct)
                            
                            ext_time_str = (ext_t + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
                            trade_logs.append({'EntryTimeRaw': curr['time'], 'Timeframe': tf, 'Type': f'BUY (Lvl {lvl})', 'Entry Time': ent_time_str, 'Exit Time': ext_time_str, 'Entry Price': round(act_ent, 2), 'TP': tp_p, 'SL': sl_p, 'Outcome': outcome, 'Profit ($)': p_usd})
                        elif not can_enter:
                            trade_logs.append({'EntryTimeRaw': curr['time'], 'Timeframe': tf, 'Type': f'BUY BLOCKED (Lvl {lvl})', 'Entry Time': ent_time_str, 'Exit Time': '---', 'Entry Price': curr['close'], 'TP': '---', 'SL': '---', 'Outcome': 'REJECTED (Trend)', 'Profit ($)': 0.0})
                
                if sell_triggers and not entered_this_candle:
                    for lvl in sell_triggers:
                        is_deep = (lvl == 90)
                        can_enter = (not bot_state['use_trend_filter']) or (bot_state['use_smart_filter'] and is_deep) or s_ema
                        ent_time_str = (curr['time'] + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
                        
                        if can_enter and not entered_this_candle:
                            entered_this_candle = True
                            act_ent = curr['close'] - (bot_state['spread_pips'] * bot_state['pip_value'])
                            tp_p = round(act_ent - (bot_state['tp_pips'][tf] * bot_state['pip_value']), 2)
                            sl_p = round(act_ent + (bot_state['sl_pips'][tf] * bot_state['pip_value']), 2)
                            eff_tp_p = tp_p + (bot_state['tp_tolerance_pips'] * bot_state['pip_value'])
                            
                            max_ext = min(curr['time'] + timedelta(hours=72), datetime.now(timezone.utc))
                            v_cands = await fetch_oanda_candles('XAU_USD', '1m', 4320, max_ext)
                            outcome, ext_t = "EXPIRED", max_ext
                            be_activated = False
                            be_target = act_ent - (20 * bot_state['pip_value'])
                            
                            for c in [v for v in v_cands if curr['time'] <= v['time'] <= max_ext]:
                                if bot_state['use_be'] and not be_activated and c['low'] <= be_target: sl_p = act_ent; be_activated = True
                                if c['high'] >= sl_p: outcome, ext_t = ("BREAK-EVEN" if be_activated and sl_p == act_ent else "LOSS"), c['time']; break
                                if c['low'] <= eff_tp_p: outcome, ext_t = "WIN", c['time']; break
                            
                            p_usd = 0.0
                            if outcome in ["WIN", "LOSS"]: 
                                p_usd = round(abs(act_ent - (tp_p if outcome=="WIN" else sl_p)) * 100 * bot_state['lot_size'], 2) * (1 if outcome=="WIN" else -1)
                            
                            if p_usd > 0: total_gross_profit += p_usd
                            elif p_usd < 0: total_gross_loss += abs(p_usd)
                            
                            current_equity = sim_balance + total_gross_profit - total_gross_loss
                            peak_equity = max(peak_equity, current_equity)
                            dd_pct = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0
                            max_dd_pct = max(max_dd_pct, dd_pct)
                            
                            ext_time_str = (ext_t + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
                            trade_logs.append({'EntryTimeRaw': curr['time'], 'Timeframe': tf, 'Type': f'SELL (Lvl {lvl})', 'Entry Time': ent_time_str, 'Exit Time': ext_time_str, 'Entry Price': round(act_ent, 2), 'TP': tp_p, 'SL': sl_p, 'Outcome': outcome, 'Profit ($)': p_usd})
                        elif not can_enter:
                            trade_logs.append({'EntryTimeRaw': curr['time'], 'Timeframe': tf, 'Type': f'SELL BLOCKED (Lvl {lvl})', 'Entry Time': ent_time_str, 'Exit Time': '---', 'Entry Price': curr['close'], 'TP': '---', 'SL': '---', 'Outcome': 'REJECTED (Trend)', 'Profit ($)': 0.0})

        if trade_logs:
            # ترتيب زمني تصاعدي حقيقي
            trade_logs.sort(key=lambda x: x['EntryTimeRaw'])
            # مسح عمود الوقت الخام قبل إنشاء ملف الـ CSV
            for log in trade_logs: del log['EntryTimeRaw']
            
            net_profit = total_gross_profit - total_gross_loss
            
            df_logs = pd.DataFrame(trade_logs)
            summary_rows = [
                {'Timeframe': '--- SUMMARY ---', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': '', 'TP': '', 'SL': '', 'Outcome': '', 'Profit ($)': ''},
                {'Timeframe': 'إجمالي الأرباح', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': '', 'TP': '', 'SL': '', 'Outcome': f'+${round(total_gross_profit, 2)}', 'Profit ($)': ''},
                {'Timeframe': 'إجمالي الخسائر', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': '', 'TP': '', 'SL': '', 'Outcome': f'-${round(total_gross_loss, 2)}', 'Profit ($)': ''},
                {'Timeframe': 'الربح الصافي', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': '', 'TP': '', 'SL': '', 'Outcome': f'${round(net_profit, 2)}', 'Profit ($)': ''},
                {'Timeframe': 'أقصى تراجع (DD)', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': '', 'TP': '', 'SL': '', 'Outcome': f'{round(max_dd_pct*100, 2)}%', 'Profit ($)': ''}
            ]
            pd.concat([df_logs, pd.DataFrame(summary_rows)]).to_csv(csv_filename, index=False)
            
            cap = f"📊 <b>نتائج الباك تيست</b>\\n\\n🟢 إجمالي الأرباح: <b>${round(total_gross_profit, 2)}</b>\\n🔴 إجمالي الخسائر: <b>${round(total_gross_loss, 2)}</b>\\n✨ الربح الصافي: <b>${round(net_profit, 2)}</b>\\n📉 أقصى تراجع: <b>{round(max_dd_pct*100, 2)}%</b>\\n\\nتم ترتيب الصفقات زمنياً لتسهيل المراجعة."
            await send_tg_document(csv_filename, cap)
            os.remove(csv_filename)
        else: 
            await send_tg_msg("⚠️ لم يتم العثور على أي صفقات في هذه الفترة.")
    except Exception as e: 
        c_log(f"❌ خطأ باك تيست: {e}")
        await send_tg_msg(f"❌ خطأ: {e}")
    finally: 
        bot_state['is_backtesting'] = False

"""
    new_content = content[:start_idx] + new_func + content[end_idx:]
    with open('telebot.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("✅ تم تحديث دالة الباك تيست بنجاح في ملف telebot.py!")
else:
    print("❌ لم أتمكن من العثور على بداية أو نهاية الدالة القديمة. تأكد من أن الملف هو telebot.py")

