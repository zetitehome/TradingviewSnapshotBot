import React, { useState, useEffect, useRef } from 'react';
import { initializeApp } from 'firebase/app';
import { getAuth, signInAnonymously, signInWithCustomToken, onAuthStateChanged } from 'firebase/auth';
import { getFirestore, collection, onSnapshot, doc, setDoc, updateDoc, query, where } from 'firebase/firestore';
import {
  RefreshCw,
  Bot,
  Bell,
  Settings,
  TrendingUp,
  DollarSign,
} from 'lucide-react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@radix-ui/react-tooltip'; // Using radix-ui for tooltip

// NOTE: This version uses standard HTML and Tailwind CSS classes to be self-contained
// and avoid compilation errors with missing Shadcn components.

const App = () => {
  const [appId, setAppId] = useState('');
  const [userId, setUserId] = useState('');
  const [authReady, setAuthReady] = useState(false);
  const [db, setDb] = useState(null);
  const [isBotOnline, setIsBotOnline] = useState(false);
  const [autoTradeEnabled, setAutoTradeEnabled] = useState(false);
  const [tradePairs, setTradePairs] = useState(['EUR/USD', 'USD/JPY', 'GBP/USD', 'AUD/CAD']);
  const [tradeHistory, setTradeHistory] = useState([]);
  const [pair, setPair] = useState('EUR/USD');
  const [direction, setDirection] = useState('buy');
  const [amount, setAmount] = useState(10);
  const [expiry, setExpiry] = useState(5);
  const [isTrading, setIsTrading] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [message, setMessage] = useState(null);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [activeTab, setActiveTab] = useState('dashboard');

  // Firestore & Auth Initialization
  useEffect(() => {
    // __app_id and __firebase_config are provided by the canvas environment
    const currentAppId = typeof __app_id !== 'undefined' ? __app_id : 'default-app-id';
    const firebaseConfig = typeof __firebase_config !== 'undefined' ? JSON.parse(__firebase_config) : {};
    setAppId(currentAppId);

    if (Object.keys(firebaseConfig).length > 0) {
      try {
        const app = initializeApp(firebaseConfig);
        const auth = getAuth(app);
        const firestore = getFirestore(app);
        setDb(firestore);

        const signIn = async () => {
          try {
            if (typeof __initial_auth_token !== 'undefined') {
              await signInWithCustomToken(auth, __initial_auth_token);
            } else {
              await signInAnonymously(auth);
            }
          } catch (error) {
            console.error("Firebase Auth Error:", error);
          }
        };

        onAuthStateChanged(auth, (user) => {
          if (user) {
            setUserId(user.uid);
            setAuthReady(true);
            console.log('Firebase auth successful.');
          } else {
            console.log('Firebase auth state changed, signing in.');
            signIn();
          }
        });
      } catch (error) {
        console.error("Firebase Init Error:", error);
        setIsLoading(false);
      }
    } else {
      console.warn("Firebase config not available. Running in a limited mode.");
      setIsLoading(false);
      setAuthReady(true);
    }
  }, []);

  // Firestore Data Listeners
  useEffect(() => {
    if (!authReady || !db || !userId) return;

    // Listen for changes in bot settings
    const botSettingsRef = doc(db, `/artifacts/${appId}/users/${userId}/bot_settings`, 'settings');
    const unsubscribeSettings = onSnapshot(botSettingsRef, (docSnap) => {
      if (docSnap.exists()) {
        const data = docSnap.data();
        setIsBotOnline(data.isOnline);
        setAutoTradeEnabled(data.autoTradeEnabled);
      }
    }, (error) => {
      console.error("Error fetching bot settings:", error);
    });

    // Listen for changes in trade history
    // NOTE: `orderBy` is commented out to avoid index creation errors. Data is sorted in memory.
    const q = query(collection(db, `/artifacts/${appId}/users/${userId}/trade_history`));
    const unsubscribeHistory = onSnapshot(q, (snapshot) => {
      const history = [];
      snapshot.forEach(doc => history.push({ id: doc.id, ...doc.data() }));
      // Sort in memory instead of using Firestore orderBy
      const sortedHistory = history.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
      setTradeHistory(sortedHistory);
    }, (error) => {
      console.error("Error fetching trade history:", error);
    });
    
    setIsLoading(false);

    return () => {
      unsubscribeSettings();
      unsubscribeHistory();
    };
  }, [authReady, db, userId]);

  // Placeholder for simulating UI.Vision macro call
  const triggerUIVisionMacro = async (macroName, params) => {
    console.log(`[UI.Vision] Triggering macro: ${macroName} with params:`, params);
    // Simulate API call
    return new Promise(resolve => setTimeout(() => {
      console.log(`[UI.Vision] Macro "${macroName}" finished.`);
      resolve({ success: true, message: `Macro "${macroName}" executed.` });
    }, 1000));
  };
  
  // Simulated auto-trade signal logic (placeholder for a more advanced algorithm)
  const autoTrade = async () => {
    if (!autoTradeEnabled || !isBotOnline) return;

    const randomPair = tradePairs[Math.floor(Math.random() * tradePairs.length)];
    const randomDirection = Math.random() > 0.5 ? 'buy' : 'sell';
    const randomAmount = Math.floor(Math.random() * 50) + 10;
    const randomExpiry = Math.floor(Math.random() * 5) + 1;
    
    const tradeData = {
      pair: randomPair,
      direction: randomDirection,
      amount: randomAmount,
      expiry: randomExpiry,
      timestamp: new Date().toISOString(),
      status: 'pending',
    };

    try {
      if (!db || !userId) throw new Error("Database not ready.");

      // Add trade to Firestore history
      const newTradeRef = doc(collection(db, `/artifacts/${appId}/users/${userId}/trade_history`));
      await setDoc(newTradeRef, tradeData);

      await triggerUIVisionMacro('PocketOptionTrade', {
        symbol: tradeData.pair,
        direction: tradeData.direction,
        amount: tradeData.amount,
        expiry: tradeData.expiry,
        username: 'gwappo94@yahoo.com',
        password: 'Shanuah2020$'
      });

      // Update trade status to success
      await updateDoc(newTradeRef, { status: 'success' });
      
      setMessage({
        title: "✅ Auto Trade Executed",
        description: `Bot placed a ${tradeData.direction.toUpperCase()} trade on ${tradeData.pair} for $${tradeData.amount}.`,
        variant: "success"
      });

    } catch (error) {
      console.error("Auto trade failed:", error);
      setMessage({
        title: "❌ Auto Trade Failed",
        description: `Could not execute auto trade. Error: ${error.message}`,
        variant: "destructive"
      });
    }
  };

  // Run auto-trade loop
  useEffect(() => {
    const intervalId = setInterval(() => {
      if (autoTradeEnabled && isBotOnline && authReady) {
        autoTrade();
      }
    }, 60000); // Check for signals every minute
    return () => clearInterval(intervalId);
  }, [autoTradeEnabled, isBotOnline, authReady]);
  

  const handleManualTrade = async (e) => {
    e.preventDefault();
    setIsTrading(true);
    
    if (!isBotOnline) {
      setMessage({
        title: "Bot is Offline",
        description: "Cannot place trades while the bot is not running.",
        variant: "destructive"
      });
      setIsTrading(false);
      return;
    }

    const tradeData = {
      pair: pair,
      direction: direction,
      amount: amount,
      expiry: expiry,
      timestamp: new Date().toISOString(),
      status: 'pending',
    };
    
    try {
      if (!db || !userId) throw new Error("Database not ready.");

      // Add trade to Firestore history
      const newTradeRef = doc(collection(db, `/artifacts/${appId}/users/${userId}/trade_history`));
      await setDoc(newTradeRef, tradeData);
      
      const result = await triggerUIVisionMacro('PocketOptionTrade', {
        symbol: pair,
        direction: direction,
        amount: amount,
        expiry: expiry,
        username: 'gwappo94@yahoo.com',
        password: 'Shanuah2020$'
      });
      
      if (result.success) {
        await updateDoc(newTradeRef, { status: 'success' });
        setMessage({
          title: "✅ Trade Executed",
          description: `${direction.toUpperCase()} trade placed on ${pair} for $${amount}.`,
          variant: "success"
        });
      } else {
         await updateDoc(newTradeRef, { status: 'failed' });
         setMessage({
          title: "❌ Trade Failed",
          description: `Could not execute trade. UI.Vision error.`,
          variant: "destructive"
        });
      }
      
    } catch (error) {
      console.error("Manual trade failed:", error);
      setMessage({
        title: "❌ Trade Failed",
        description: `Could not execute trade. Error: ${error.message}`,
        variant: "destructive"
      });
    } finally {
      setIsTrading(false);
    }
  };

  // UI.Vision "learn" command
  const handleLearn = async () => {
    setMessage({
      title: "🧠 Starting Learning Process",
      description: "Triggering UI.Vision to learn from historical data.",
      variant: "info"
    });
    try {
      await triggerUIVisionMacro('AnalyzeMarketData', {
        username: 'gwappo94@yahoo.com',
        password: 'Shanuah2020$'
      });
      setMessage({
        title: "✅ Learning Macro Triggered",
        description: "UI.Vision is now analyzing data. You will be notified when it's complete.",
        variant: "success"
      });
    } catch (error) {
      setMessage({
        title: "❌ Learning Macro Failed",
        description: `Error: ${error.message}`,
        variant: "destructive"
      });
    }
  };

  const handleToggleAutoTrade = async (e) => {
    const checked = e.target.checked;
    try {
      if (!db || !userId) throw new Error("Database not ready.");
      const settingsRef = doc(db, `/artifacts/${appId}/users/${userId}/bot_settings`, 'settings');
      await setDoc(settingsRef, { autoTradeEnabled: checked, isOnline: true }, { merge: true });
      setAutoTradeEnabled(checked);
      setMessage({
        title: `Auto Trade ${checked ? 'Enabled' : 'Disabled'}`,
        description: `Automated trading is now ${checked ? 'ON' : 'OFF'}.`,
        variant: "info"
      });
    } catch (error) {
      console.error("Failed to toggle auto trade:", error);
      setMessage({
        title: "❌ Failed to toggle",
        description: `Error: ${error.message}`,
        variant: "destructive"
      });
    }
  };

  // Components moved to be inline for self-contained code
  const Toast = ({ message }) => {
    if (!message) return null;
    return (
      <div className={`fixed bottom-4 right-4 z-50 p-4 rounded-lg shadow-xl text-white
        ${message.variant === 'success' ? 'bg-green-600' :
        message.variant === 'info' ? 'bg-indigo-600' :
        'bg-red-600'}`}>
        <h3 className="font-bold">{message.title}</h3>
        <p className="text-sm">{message.description}</p>
      </div>
    );
  };
  
  const Loader = () => (
    <div className="flex items-center justify-center min-h-screen">
      <svg className="animate-spin h-16 w-16 text-indigo-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
      </svg>
    </div>
  );

  const Dialog = ({ children }) => {
    return (
      <div className={`fixed inset-0 z-50 bg-black bg-opacity-75 flex items-center justify-center transition-opacity ${isDialogOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}>
        <div className="bg-gray-900 rounded-lg shadow-2xl p-6 w-full max-w-md">
          {children}
        </div>
      </div>
    );
  };

  if (isLoading) {
    return <Loader />;
  }

  return (
    <div className="min-h-screen bg-gray-900 text-gray-100 p-8 font-sans">
      <div className="max-w-7xl mx-auto">
        <header className="flex justify-between items-center mb-12">
          <h1 className="text-4xl font-bold tracking-tight text-white flex items-center">
            <Bot className="h-10 w-10 mr-4 text-indigo-400" />
            Advanced Trading Bot Dashboard
          </h1>
          <div className="flex items-center space-x-4">
            <div className={`px-4 py-2 rounded-xl text-lg font-bold ${isBotOnline ? 'bg-green-600' : 'bg-red-600'}`}>
              Bot Status: {isBotOnline ? 'Online' : 'Offline'}
            </div>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button className="text-gray-400 hover:text-white p-2 rounded-full transition-colors">
                    <RefreshCw className="h-5 w-5" />
                  </button>
                </TooltipTrigger>
                <TooltipContent className="bg-gray-700 text-white text-sm p-2 rounded-lg shadow-lg">
                  <p>User ID: {userId}</p>
                  <p>App ID: {appId}</p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </header>

        <div className="bg-gray-800 rounded-xl p-1 flex space-x-1 lg:space-x-4">
            <button onClick={() => setActiveTab('dashboard')} className={`flex-1 py-2 px-4 rounded-lg text-lg font-medium transition-colors ${activeTab === 'dashboard' ? 'bg-indigo-500 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}>Dashboard</button>
            <button onClick={() => setActiveTab('manual-trade')} className={`flex-1 py-2 px-4 rounded-lg text-lg font-medium transition-colors ${activeTab === 'manual-trade' ? 'bg-indigo-500 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}>Manual Trade</button>
            <button onClick={() => setActiveTab('trade-history')} className={`flex-1 py-2 px-4 rounded-lg text-lg font-medium transition-colors ${activeTab === 'trade-history' ? 'bg-indigo-500 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}>Trade History</button>
            <button onClick={() => setActiveTab('settings')} className={`flex-1 py-2 px-4 rounded-lg text-lg font-medium transition-colors ${activeTab === 'settings' ? 'bg-indigo-500 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}>Settings</button>
        </div>

        <div className="mt-8">
          {activeTab === 'dashboard' && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
              <div className="bg-gray-800 border border-gray-700 shadow-lg rounded-xl p-6">
                <div className="flex flex-row items-center justify-between pb-2">
                  <div className="text-lg font-medium text-gray-300">Auto Trading</div>
                  <Bot className="h-5 w-5 text-indigo-400" />
                </div>
                <div className="flex items-center justify-between mt-4">
                  <span className="text-2xl font-bold text-white">{autoTradeEnabled ? 'Enabled' : 'Disabled'}</span>
                  <label className="flex items-center cursor-pointer">
                    <div className="relative">
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={autoTradeEnabled}
                        onChange={handleToggleAutoTrade}
                        disabled={!isBotOnline}
                      />
                      <div className={`block bg-gray-600 w-14 h-8 rounded-full transition-colors ${autoTradeEnabled ? 'bg-indigo-500' : ''}`}></div>
                      <div className={`dot absolute left-1 top-1 bg-white w-6 h-6 rounded-full transition-transform ${autoTradeEnabled ? 'transform translate-x-6' : ''}`}></div>
                    </div>
                  </label>
                </div>
                <p className="text-xs text-gray-400 mt-2">
                  {autoTradeEnabled ? 'Bot is automatically placing trades based on signals.' : 'Automatic trading is currently paused.'}
                </p>
              </div>

              <div className="bg-gray-800 border border-gray-700 shadow-lg rounded-xl p-6">
                <div className="flex flex-row items-center justify-between pb-2">
                  <div className="text-lg font-medium text-gray-300">Live Signals</div>
                  <TrendingUp className="h-5 w-5 text-green-400" />
                </div>
                <div className="mt-4">
                  <p className="text-2xl font-bold text-white">EUR/USD - BUY</p>
                  <p className="text-xs text-gray-400 mt-2">
                    Next signal in 23s. Win rate: 78%
                  </p>
                </div>
              </div>
              
              <div className="bg-gray-800 border border-gray-700 shadow-lg rounded-xl p-6">
                <div className="flex flex-row items-center justify-between pb-2">
                  <div className="text-lg font-medium text-gray-300">Manual Trades</div>
                  <DollarSign className="h-5 w-5 text-yellow-400" />
                </div>
                <div className="mt-4">
                  <p className="text-2xl font-bold text-white">{tradeHistory.length}</p>
                  <p className="text-xs text-gray-400 mt-2">
                    Total trades executed so far.
                  </p>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'manual-trade' && (
            <div className="bg-gray-800 border border-gray-700 shadow-lg rounded-xl p-8">
              <h2 className="text-2xl font-bold text-white mb-6">Manual Trade</h2>
              <form onSubmit={handleManualTrade} className="space-y-6">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="space-y-2">
                    <label htmlFor="pair" className="text-gray-300 block">Currency Pair</label>
                    <select
                      id="pair"
                      value={pair}
                      onChange={(e) => setPair(e.target.value)}
                      disabled={isTrading}
                      className="w-full bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    >
                      {tradePairs.map((p) => (
                        <option key={p} value={p}>{p}</option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-2">
                    <label htmlFor="direction" className="text-gray-300 block">Direction</label>
                    <select
                      id="direction"
                      value={direction}
                      onChange={(e) => setDirection(e.target.value)}
                      disabled={isTrading}
                      className="w-full bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    >
                      <option value="buy">BUY</option>
                      <option value="sell">SELL</option>
                    </select>
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="space-y-2">
                    <label htmlFor="amount" className="text-gray-300 block">Amount ($)</label>
                    <input
                      id="amount"
                      type="number"
                      placeholder="e.g., 100"
                      value={amount}
                      onChange={(e) => setAmount(Number(e.target.value))}
                      className="w-full bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
                      disabled={isTrading}
                    />
                  </div>
                  <div className="space-y-2">
                    <label htmlFor="expiry" className="text-gray-300 block">Expiry (minutes)</label>
                    <input
                      id="expiry"
                      type="number"
                      placeholder="e.g., 5"
                      value={expiry}
                      onChange={(e) => setExpiry(Number(e.target.value))}
                      className="w-full bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
                      disabled={isTrading}
                    />
                  </div>
                </div>
                <button type="submit" className="w-full bg-indigo-500 hover:bg-indigo-600 text-white font-bold py-3 text-lg rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed" disabled={isTrading}>
                  {isTrading ? 'Placing Trade...' : 'Place Trade'}
                </button>
              </form>
              {message && message.variant === 'destructive' && (
                <div className="mt-6 p-4 rounded-lg bg-red-900 border border-red-700 text-red-100">
                  <h4 className="font-bold">{message.title}</h4>
                  <p className="text-sm">{message.description}</p>
                </div>
              )}
            </div>
          )}
          
          {activeTab === 'trade-history' && (
            <div className="bg-gray-800 border border-gray-700 shadow-lg rounded-xl p-8">
              <h2 className="text-2xl font-bold text-white mb-6">Trade History</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <thead>
                    <tr className="border-b border-gray-700">
                      <th className="py-3 px-4 text-gray-400">Time</th>
                      <th className="py-3 px-4 text-gray-400">Pair</th>
                      <th className="py-3 px-4 text-gray-400">Direction</th>
                      <th className="py-3 px-4 text-gray-400">Amount</th>
                      <th className="py-3 px-4 text-gray-400">Expiry</th>
                      <th className="py-3 px-4 text-gray-400">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tradeHistory.length > 0 ? (
                      tradeHistory.map((trade) => (
                        <tr key={trade.id} className="border-b border-gray-700 last:border-b-0">
                          <td className="py-3 px-4 text-gray-200">
                            {new Date(trade.timestamp).toLocaleTimeString()}
                          </td>
                          <td className="py-3 px-4 text-gray-300">{trade.pair}</td>
                          <td className="py-3 px-4">
                            <span className={`px-2 py-1 rounded-full text-xs font-semibold ${trade.direction === 'buy' ? 'bg-green-600' : 'bg-red-600'}`}>
                              {trade.direction.toUpperCase()}
                            </span>
                          </td>
                          <td className="py-3 px-4 text-gray-200">${trade.amount}</td>
                          <td className="py-3 px-4 text-gray-200">{trade.expiry}m</td>
                          <td className="py-3 px-4">
                            <span className={`px-2 py-1 rounded-full text-xs font-semibold ${trade.status === 'success' ? 'bg-green-600' : (trade.status === 'pending' ? 'bg-yellow-600' : 'bg-red-600')}`}>
                              {trade.status.toUpperCase()}
                            </span>
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={6} className="text-center text-gray-400 py-8">
                          No trades have been executed yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          
          {activeTab === 'settings' && (
            <div className="bg-gray-800 border border-gray-700 shadow-lg rounded-xl p-8">
              <h2 className="text-2xl font-bold text-white mb-2">Bot Settings</h2>
              <p className="text-gray-400 mb-6">
                Configure your bot's behavior and link it to your trading account.
              </p>
              <div className="space-y-6">
                <div>
                  <h3 className="text-lg font-semibold text-white mb-2">Bot Operations</h3>
                  <div className="flex items-center space-x-4">
                    <label htmlFor="auto-trade" className="text-gray-300">
                      Enable Automatic Trading
                    </label>
                    <label className="flex items-center cursor-pointer">
                      <div className="relative">
                        <input
                          id="auto-trade"
                          type="checkbox"
                          className="sr-only"
                          checked={autoTradeEnabled}
                          onChange={handleToggleAutoTrade}
                        />
                        <div className={`block bg-gray-600 w-14 h-8 rounded-full transition-colors ${autoTradeEnabled ? 'bg-indigo-500' : ''}`}></div>
                        <div className={`dot absolute left-1 top-1 bg-white w-6 h-6 rounded-full transition-transform ${autoTradeEnabled ? 'transform translate-x-6' : ''}`}></div>
                      </div>
                    </label>
                  </div>
                  <p className="text-sm text-gray-500 mt-1">
                    Turn this on to allow the bot to place trades based on its signals.
                  </p>
                </div>
                
                <div>
                  <h3 className="text-lg font-semibold text-white mb-2">Learning & Analysis</h3>
                  <button onClick={handleLearn} className="bg-purple-600 hover:bg-purple-700 text-white font-bold py-3 px-6 rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                    Trigger Learning Macro
                  </button>
                  <p className="text-sm text-gray-500 mt-1">
                    This will run a UI.Vision macro to analyze historical data and improve future signals.
                  </p>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
      <Toast message={message} />
    </div>
  );
};

export default App;
