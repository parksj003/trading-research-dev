"""Prior ten-session low research exit, not a full Turtle trading system."""
import pandas as pd


def donchian10(frame,valid):
    # Shift BEFORE rolling: today's Low can only affect tomorrow's channel.
    return pd.to_numeric(frame.Low,errors='coerce').where(valid).shift(1).rolling(10,min_periods=10).min()


def sell_stop(initial,channel):
    # Higher sell-stop is encountered first on a downward path; fixed wins ties.
    return (channel,'donchian_stop') if channel>initial else (initial,'initial_stop')

