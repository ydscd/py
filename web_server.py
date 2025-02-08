import dash
from dash import dcc, html
import dash_bootstrap_components as dbc
from flask import Flask
from dash.dependencies import Input, Output

def create_dash_app(get_monitor):
    # 使用 Flask 作为底层服务器
    server = Flask(__name__)
    app = dash.Dash(__name__, server=server, external_stylesheets=[dbc.themes.BOOTSTRAP])

    # 页面布局：无需单独建 HTML 文件，全部在 Dash 中定义
    app.layout = dbc.Container([
        html.H1("加密货币监控系统", className="text-center mt-3"),
        html.Hr(),
        html.H3("交易所状态"),
        dcc.Interval(id="interval-component", interval=5000, n_intervals=0),  # 每5秒刷新
        dbc.Table(id="exchange-status-table", bordered=True, striped=True, hover=True),
        html.H3("交易排行榜"),
        dbc.Table(id="leaderboard-table", bordered=True, striped=True, hover=True)
    ], fluid=True)

    # 回调函数：更新交易所状态
    @app.callback(
        Output("exchange-status-table", "children"),
        Input("interval-component", "n_intervals")
    )
    def update_status(n):
        monitor = get_monitor()
        if not monitor:
            return [html.Tr([html.Td("暂无数据")])]
        status = monitor.exchange_status
        header = html.Thead(html.Tr([html.Th("交易所"), html.Th("状态")]))
        body = html.Tbody([html.Tr([html.Td(ex), html.Td(status.get(ex, "unknown"))])
                            for ex in status])
        return [header, body]

    # 回调函数：更新排行榜
    @app.callback(
        Output("leaderboard-table", "children"),
        Input("interval-component", "n_intervals")
    )
    def update_leaderboard(n):
        monitor = get_monitor()
        if not monitor:
            return [html.Tr([html.Td("暂无数据")])]
        leaderboard = monitor.get_leaderboard()
        header = html.Thead(html.Tr([html.Th("交易对"), html.Th("涨跌幅(%)")]))
        body = html.Tbody([html.Tr([html.Td(pair), html.Td(f"{change:.2f}")])
                            for pair, change in leaderboard])
        return [header, body]

    return app

def run_web_server(get_monitor, host="0.0.0.0", port=5000):
    app = create_dash_app(get_monitor)
    app.run_server(host=host, port=port, debug=False)
