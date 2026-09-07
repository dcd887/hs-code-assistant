// pages/history/history.js
const app = getApp()

Page({
  data: {
    history: []
  },

  onShow() {
    const history = wx.getStorageSync('searchHistory') || []
    this.setData({ history })
  },

  onItemTap(e) {
    const item = e.currentTarget.dataset.item
    wx.navigateTo({
      url: `/pages/result/result?description=${encodeURIComponent(item.description)}`
    })
  },

  onClear() {
    wx.showModal({
      title: '确认清空',
      content: '确定要清空所有搜索历史吗？',
      success: (res) => {
        if (res.confirm) {
          wx.removeStorageSync('searchHistory')
          app.globalData.history = []
          this.setData({ history: [] })
        }
      }
    })
  },

  goSearch() {
    wx.switchTab({
      url: '/pages/index/index',
      fail: () => {
        wx.navigateBack()
      }
    })
  }
})
