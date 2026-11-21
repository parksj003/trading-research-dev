"""Pure common OHLC/MA traces. No app, Streamlit, price fetch or trade signals."""
import plotly.graph_objects as go

MA_COLORS={20:'#2563EB',50:'#D97706',200:'#7C3AED'}


def add_price_traces(fig,ch,bars,compact,up,down,ma_colors=MA_COLORS,kind='candles'):
    d=ch['d'][-bars:]
    if kind=='candles':
        fig.add_trace(go.Candlestick(x=d,open=ch['o'][-bars:],high=ch['h'][-bars:],low=ch['l'][-bars:],close=ch['c'][-bars:],name='주가',
            increasing_line_color=up,increasing_fillcolor=up,decreasing_line_color=down,decreasing_fillcolor=down),row=1,col=1)
    else:fig.add_trace(go.Scatter(x=d,y=ch['c'][-bars:],name='가격',mode='lines',line=dict(color=up)),row=1,col=1)
    for window in (20,50,200):
        key='s'+str(window)
        if key in ch and ch[key]:
            fig.add_trace(go.Scatter(x=d,y=ch[key][-bars:],name=f'{window}일',mode='lines',
                line=dict(width=1.2 if compact else 1.5,color=ma_colors[window])),row=1,col=1)
    return fig

