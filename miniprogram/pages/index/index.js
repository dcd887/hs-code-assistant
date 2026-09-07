// pages/index/index.js
const app = getApp()

Page({
  data: {
    description: '',
    loading: false,
    hotKeywords: ['笔记本电脑', '纯棉T恤', '不锈钢锅', '智能手机', '运动鞋', '口红', '咖啡'],
    recentHistory: []
  },

  onShow() {
    const history = wx.getStorageSync('searchHistory') || []
    this.setData({ recentHistory: history.slice(0, 5) })
  },

  onInput(e) {
    this.setData({ description: e.detail.value })
  },

  onHotTap(e) {
    this.setData({ description: e.currentTarget.dataset.keyword })
    this.onSearch()
  },

  onHistoryTap(e) {
    const item = e.currentTarget.dataset.item
    wx.navigateTo({
      url: `/pages/result/result?description=${encodeURIComponent(item.description)}`
    })
  },

  onSearch() {
    const desc = this.data.description.trim()
    if (!desc) {
      wx.showToast({ title: '请输入商品描述', icon: 'none' })
      return
    }
    this.setData({ loading: true })
    wx.navigateTo({
      url: `/pages/result/result?description=${encodeURIComponent(desc)}`,
      success: () => {
        this.setData({ loading: false, description: '' })
      }
    })
  }
})
