// app.js
App({
  globalData: {
    apiBase: 'http://192.168.10.205:8000', // 开发环境用电脑局域网IP，上线改为云托管地址
    history: []
  },
  onLaunch() {
    // 加载历史记录
    const history = wx.getStorageSync('searchHistory') || []
    this.globalData.history = history
  },
  addHistory(item) {
    this.globalData.history.unshift({
      ...item,
      time: new Date().toLocaleString()
    })
    if (this.globalData.history.length > 50) {
      this.globalData.history = this.globalData.history.slice(0, 50)
    }
    wx.setStorageSync('searchHistory', this.globalData.history)
  }
})
