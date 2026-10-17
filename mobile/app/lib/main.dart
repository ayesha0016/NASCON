import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:hive_flutter/hive_flutter.dart';
import 'package:http/http.dart' as http;

// ============================================================================
// Local storage: Hive (cache + offline queue + settings), Keystore for token.
// ============================================================================
late Box cacheBox;
late Box queueBox;
late Box settingsBox;
const secure = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true));

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Hive.initFlutter();
  cacheBox = await Hive.openBox('cache');
  queueBox = await Hive.openBox('queue');
  settingsBox = await Hive.openBox('settings');
  final app = AppState();
  await app.init();
  runApp(NasconApp(app: app));
}

// ============================================================================
// App state: connectivity, auth, cached data, offline queue + sync.
// ============================================================================
class AppState extends ChangeNotifier {
  String baseUrl = kIsWeb ? 'http://localhost:8080' : 'http://10.0.2.2:8080';
  String? token;
  String role = '';
  String email = '';
  bool online = false;
  bool syncing = false;
  Map<String, dynamic>? data;
  List<Map<String, dynamic>> lastSyncResults = [];
  Timer? _timer;
  String deviceId = '';

  List<Map<String, dynamic>> get queue => queueBox.values
      .map((e) => Map<String, dynamic>.from(jsonDecode(e as String)))
      .toList();

  Future<void> init() async {
    baseUrl = settingsBox.get('baseUrl', defaultValue: baseUrl) as String;
    if (kIsWeb && baseUrl.contains('10.0.2.2')) {
      baseUrl = 'http://localhost:8080';
      await settingsBox.put('baseUrl', baseUrl);
    }
    deviceId = settingsBox.get('deviceId', defaultValue: '') as String;
    if (deviceId.isEmpty) {
      deviceId = 'dev-${Random().nextInt(1 << 31)}';
      await settingsBox.put('deviceId', deviceId);
    }
    token = await secure.read(key: 'token');
    role = settingsBox.get('role', defaultValue: '') as String;
    email = settingsBox.get('email', defaultValue: '') as String;
    final cached = cacheBox.get('bootstrap');
    if (cached != null)
      data = Map<String, dynamic>.from(jsonDecode(cached as String));
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => checkOnline());
    await checkOnline();
  }

  Future<void> setBaseUrl(String url) async {
    baseUrl = url.trim().replaceAll(RegExp(r'/+$'), '');
    if (!baseUrl.startsWith('http')) baseUrl = 'http://$baseUrl';
    await settingsBox.put('baseUrl', baseUrl);
    await checkOnline();
  }

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (token != null) 'Authorization': 'Bearer $token',
      };

  Future<ApiResult> get(String path) async {
    try {
      final r = await http
          .get(Uri.parse('$baseUrl$path'), headers: _headers)
          .timeout(const Duration(seconds: 6));
      _setOnline(true);
      return ApiResult(r.statusCode, _decode(r.body));
    } catch (_) {
      _setOnline(false);
      return ApiResult(0, null);
    }
  }

  Future<ApiResult> post(String path, Map<String, dynamic> body) async {
    try {
      final r = await http
          .post(Uri.parse('$baseUrl$path'),
              headers: _headers, body: jsonEncode(body))
          .timeout(const Duration(seconds: 10));
      _setOnline(true);
      return ApiResult(r.statusCode, _decode(r.body));
    } catch (_) {
      _setOnline(false);
      return ApiResult(0, null);
    }
  }

  dynamic _decode(String s) {
    try {
      return jsonDecode(s);
    } catch (_) {
      return null;
    }
  }

  void _setOnline(bool v) {
    if (online != v) {
      online = v;
      notifyListeners();
      if (v && queue.isNotEmpty && token != null) syncNow();
    }
  }

  Future<void> checkOnline() async {
    try {
      final r = await http
          .get(Uri.parse('$baseUrl/health'))
          .timeout(const Duration(seconds: 3));
      _setOnline(r.statusCode == 200);
    } catch (_) {
      _setOnline(false);
    }
  }

  Future<String?> login(String mail, String password) async {
    await secure.delete(key: 'token');
    token = null;
    data = null;
    await cacheBox.delete('bootstrap');
    await cacheBox.delete('standings');
    final r = await post(
        '/api/v1/auth/login', {'email': mail.trim(), 'password': password});
    if (r.status == 0)
      return 'Cannot reach the server. Check the server address in Settings.';
    if (r.status != 200) return 'Wrong email or password. Please try again.';
    token = r.body['token'] as String;
    role = r.body['role'] as String;
    email = mail.trim();
    await secure.write(key: 'token', value: token);
    await settingsBox.put('role', role);
    await settingsBox.put('email', email);
    await refresh();
    notifyListeners();
    return null;
  }

  Future<void> logout() async {
    token = null;
    role = '';
    email = '';
    data = null;
    await secure.delete(key: 'token');
    await settingsBox.delete('role');
    await settingsBox.delete('email');
    await cacheBox.delete('bootstrap');
    await cacheBox.delete('standings');
    notifyListeners();
  }

  Future<void> refresh() async {
    final r = await get('/api/v1/app/bootstrap');
    if (r.status == 200) {
      data = Map<String, dynamic>.from(r.body);
      await cacheBox.put('bootstrap', jsonEncode(r.body));
      final lid = data!['league']['league_id'];
      final s = await get('/audit/v1/standings/$lid');
      if (s.status == 200) await cacheBox.put('standings', jsonEncode(s.body));
      notifyListeners();
    }
  }

  Map<String, dynamic>? cached(String key) {
    final v = cacheBox.get(key);
    return v == null
        ? null
        : Map<String, dynamic>.from(jsonDecode(v as String));
  }

  Future<void> putCache(String key, dynamic value) =>
      cacheBox.put(key, jsonEncode(value));

  // ---- offline queue (Section 5.14 action format) ----
  Future<void> enqueue(String type, int baseVersion,
      Map<String, dynamic> payload, String label) async {
    final id = 'a-${DateTime.now().microsecondsSinceEpoch}';
    final action = {
      'action_id': id,
      'type': type,
      'queued_at':
          DateTime.now().toUtc().toIso8601String().split('.').first + 'Z',
      'base_version': baseVersion,
      'payload': payload,
      'label': label,
    };
    await queueBox.put(id, jsonEncode(action));
    notifyListeners();
  }

  Future<List<Map<String, dynamic>>> syncNow() async {
    if (syncing || queue.isEmpty) return [];
    syncing = true;
    notifyListeners();
    final items = queue;
    final actions = items
        .map((a) => Map<String, dynamic>.from(a)..remove('label'))
        .toList();
    final r = await post(
        '/audit/v1/offline-sync', {'device_id': deviceId, 'actions': actions});
    List<Map<String, dynamic>> results = [];
    if (r.status == 200) {
      for (final res in (r.body['results'] as List)) {
        final m = Map<String, dynamic>.from(res);
        final src = items.firstWhere((a) => a['action_id'] == m['action_id'],
            orElse: () => {});
        m['label'] = src['label'] ?? m['action_id'];
        results.add(m);
        await queueBox.delete(m['action_id']);
      }
      lastSyncResults = results;
      await refresh();
    }
    syncing = false;
    notifyListeners();
    return results;
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }
}

class ApiResult {
  final int status;
  final dynamic body;
  ApiResult(this.status, this.body);
  bool get ok => status >= 200 && status < 300;
  String get message => friendlyError(this);
}

// Plain-language messages; never show raw codes alone.
String friendlyError(ApiResult r) {
  if (r.status == 0)
    return 'You are offline. The action was saved and will sync automatically.';
  final code = (r.body is Map && r.body['error'] is Map)
      ? r.body['error']['code'] as String?
      : null;
  return explainCode(code) ?? 'Something went wrong. Please try again.';
}

String? explainCode(String? code) {
  const m = {
    'OUT_OF_STOCK': 'Sorry, this size is sold out.',
    'PAYMENT_DECLINED': 'Your payment was declined. Try another card.',
    'PAYMENT_CAPTURE_FAILED':
        'Payment could not be completed. You were not charged.',
    'INVALID_DISCOUNT_CODE': 'That discount code is not valid.',
    'MATCH_NOT_IN_PROGRESS':
        'This match is not live, so scores cannot be added.',
    'ILLEGAL_TRANSITION': 'This match cannot change to that status now.',
    'TIE_NOT_ALLOWED':
        'The score is level and draws are not allowed. Record a tie-break winner first.',
    'INVALID_COMPENSATION': 'That event was already corrected.',
    'TRANSFER_WINDOW_CLOSED': 'The transfer window is closed for that date.',
    'SQUAD_FULL': 'The destination squad is already full.',
    'MIN_POSITION_VIOLATION':
        'The squad would not have enough players in that position.',
    'NOT_IN_SQUAD': 'This player is no longer in that squad.',
    'PLAYER_ON_LOAN': 'This player is on loan and cannot be moved.',
    'CROSS_TENANT_TRANSFER':
        'Players cannot move to a club in another organisation.',
    'INVALID_BASE_VERSION':
        'Your data was out of date. Please refresh and try again.',
    'DUPLICATE_EVENT_ID':
        'This action was already recorded differently on the server.',
    'MATCH_NOT_FOUND': 'That match no longer exists.',
    'PLAYER_NOT_FOUND': 'That player no longer exists.',
    'FORBIDDEN': 'You do not have permission to do this.',
    'UNAUTHENTICATED': 'Your session expired. Please log in again.',
    'VALIDATION_ERROR': 'Some details are missing or invalid.',
  };
  return code == null ? null : m[code];
}

String pkr(num minor) => 'PKR ${(minor / 100).toStringAsFixed(2)}';

String roleLabel(String r) =>
    const {
      'super_admin': 'Platform Admin',
      'league_organizer': 'League Organizer',
      'club_manager': 'Club Manager',
      'player': 'Player',
      'viewer': 'Viewer',
    }[r] ??
    r;

String roleSummary(String r) =>
    const {
      'super_admin': 'Full platform administration',
      'league_organizer': 'League operations and scoring',
      'club_manager': 'Club, roster and merchandise',
      'player': 'Player profile and club activity',
      'viewer': 'Read-only league access',
    }[r] ??
    'League operations';

// ============================================================================
// UI
// ============================================================================
class NasconApp extends StatelessWidget {
  final AppState app;
  const NasconApp({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: app,
      builder: (context, _) {
        final hex = (app.data?['tenant']?['color'] as String?) ?? '#17634B';
        final seed = Color(int.parse(hex.replaceFirst('#', 'FF'), radix: 16));
        return MaterialApp(
          title: 'NaSCon League',
          debugShowCheckedModeBanner: false,
          theme: ThemeData(
            useMaterial3: true,
            fontFamily: 'Roboto',
            colorScheme: ColorScheme.fromSeed(
                    seedColor: seed, brightness: Brightness.light)
                .copyWith(
              primary: const Color(0xFF17634B),
              onPrimary: Colors.white,
              surface: const Color(0xFFFFFCF7),
              onSurface: const Color(0xFF172B25),
              outline: const Color(0xFFD5DDD5),
            ),
            scaffoldBackgroundColor: const Color(0xFFFFFCF7),
            appBarTheme: const AppBarTheme(
                backgroundColor: Color(0xFFFFFCF7),
                surfaceTintColor: Colors.transparent,
                centerTitle: false,
                elevation: 0,
                toolbarHeight: 68,
                titleTextStyle: TextStyle(
                    color: Color(0xFF172B25),
                    fontSize: 22,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -0.4)),
            textTheme: const TextTheme(
              bodyMedium: TextStyle(
                  fontSize: 15, color: Color(0xFF243A32), height: 1.45),
              titleLarge: TextStyle(
                  fontSize: 23,
                  fontWeight: FontWeight.w800,
                  letterSpacing: -0.7,
                  color: Color(0xFF172B25)),
              titleMedium: TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                  color: Color(0xFF172B25)),
              headlineSmall: TextStyle(
                  fontSize: 27,
                  fontWeight: FontWeight.w800,
                  letterSpacing: -0.8,
                  color: Color(0xFF172B25)),
            ),
            cardTheme: CardTheme(
                color: Colors.white,
                surfaceTintColor: Colors.transparent,
                elevation: 0,
                margin: EdgeInsets.zero,
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                    side: const BorderSide(color: Color(0xFFE1E7E1)))),
            navigationBarTheme: NavigationBarThemeData(
                backgroundColor: Colors.white,
                surfaceTintColor: Colors.transparent,
                indicatorColor: seed.withOpacity(0.12),
                height: 76,
                elevation: 1,
                labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
                labelTextStyle: MaterialStateProperty.all(const TextStyle(
                    fontSize: 12, fontWeight: FontWeight.w600))),
            inputDecorationTheme: InputDecorationTheme(
                filled: true,
                fillColor: Colors.white,
                contentPadding:
                    const EdgeInsets.symmetric(horizontal: 16, vertical: 18),
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                    borderSide: const BorderSide(color: Color(0xFFD7DED8))),
                enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                    borderSide: const BorderSide(color: Color(0xFFD7DED8)))),
            filledButtonTheme: FilledButtonThemeData(
                style: FilledButton.styleFrom(
                    minimumSize: const Size(48, 52),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(8)),
                    textStyle: const TextStyle(
                        fontSize: 16, fontWeight: FontWeight.w700))),
            chipTheme: ChipThemeData(
                backgroundColor: Colors.white,
                selectedColor: seed.withOpacity(0.12),
                side: const BorderSide(color: Color(0xFFDCE3DC)),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(8)),
                padding:
                    const EdgeInsets.symmetric(horizontal: 8, vertical: 8)),
            dataTableTheme: const DataTableThemeData(
                headingRowHeight: 48,
                dataRowMinHeight: 64,
                dataRowMaxHeight: 72,
                headingTextStyle: TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: Color(0xFF65746B)),
                dividerThickness: 0.5),
            listTileTheme: const ListTileThemeData(
                contentPadding:
                    EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                iconColor: Color(0xFF52685D)),
            dividerTheme:
                const DividerThemeData(color: Color(0xFFE1E7E1), thickness: 1),
            progressIndicatorTheme:
                const ProgressIndicatorThemeData(color: Color(0xFF17634B)),
          ),
          home: app.token == null ? LoginPage(app: app) : HomePage(app: app),
        );
      },
    );
  }
}

/// Always-visible Online / Offline / Syncing banner with pending count.
class SyncBanner extends StatelessWidget {
  final AppState app;
  const SyncBanner({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final pending = app.queue.length;
    late final Color bg;
    late final IconData icon;
    late final String text;
    if (app.syncing) {
      bg = const Color(0xFFFFF1D9);
      icon = Icons.sync;
      text = 'Syncing $pending pending action(s)…';
    } else if (app.online) {
      bg = const Color(0xFFEAF3EC);
      icon = Icons.cloud_done;
      text = pending == 0 ? 'Online' : 'Online · $pending pending';
    } else {
      bg = const Color(0xFFE9ECE8);
      icon = Icons.cloud_off;
      text = pending == 0
          ? 'Offline · showing saved data'
          : 'Offline · $pending pending action(s)';
    }
    return Semantics(
      liveRegion: true,
      label: 'Connection status: $text',
      child: InkWell(
        onTap: () => Navigator.push(
            context, MaterialPageRoute(builder: (_) => QueuePage(app: app))),
        child: Container(
          width: double.infinity,
          constraints: const BoxConstraints(minHeight: 40),
          color: bg,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: Row(children: [
            Icon(icon, color: const Color(0xFF264C39), size: 20),
            const SizedBox(width: 8),
            Expanded(
                child: Text(text,
                    style: const TextStyle(
                        color: const Color(0xFF264C39),
                        fontWeight: FontWeight.w600))),
            const Text('Queue',
                style: TextStyle(
                    color: Color(0xFF264C39), fontWeight: FontWeight.w700)),
          ]),
        ),
      ),
    );
  }
}

class LoginPage extends StatefulWidget {
  final AppState app;
  const LoginPage({super.key, required this.app});
  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final emailC = TextEditingController(text: 'manager@nascon.test');
  final passC = TextEditingController(text: 'Nascon@26');
  String? error;
  bool busy = false;

  @override
  Widget build(BuildContext context) {
    final app = widget.app;
    return Scaffold(
      appBar: AppBar(title: const Text('NaSCon League'), actions: [
        IconButton(
          tooltip: 'Server settings',
          icon: const Icon(Icons.settings),
          onPressed: () => showServerDialog(context, app),
        ),
      ]),
      body: Column(children: [
        SyncBanner(app: app),
        Expanded(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 480),
              child: ListView(
                  padding: const EdgeInsets.fromLTRB(24, 32, 24, 24),
                  children: [
                    Text('Welcome back',
                        style: Theme.of(context).textTheme.headlineSmall),
                    const SizedBox(height: 6),
                    const Text(
                        'Sign in to manage your league, club and matchday operations.'),
                    const SizedBox(height: 8),
                    Text('Server  ·  ${app.baseUrl}',
                        style: Theme.of(context).textTheme.bodySmall),
                    const SizedBox(height: 28),
                    TextField(
                      controller: emailC,
                      keyboardType: TextInputType.emailAddress,
                      textInputAction: TextInputAction.next,
                      decoration: const InputDecoration(
                          labelText: 'Email',
                          prefixIcon: Icon(Icons.mail_outline)),
                    ),
                    const SizedBox(height: 14),
                    TextField(
                      controller: passC,
                      obscureText: true,
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _login(),
                      decoration: const InputDecoration(
                          labelText: 'Password',
                          prefixIcon: Icon(Icons.lock_outline)),
                    ),
                    if (error != null) ...[
                      const SizedBox(height: 12),
                      Text(error!,
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error)),
                    ],
                    const SizedBox(height: 22),
                    SizedBox(
                      height: 52,
                      child: FilledButton(
                        onPressed: busy ? null : _login,
                        child: busy
                            ? const CircularProgressIndicator()
                            : const Text('Sign in',
                                style: TextStyle(fontSize: 16)),
                      ),
                    ),
                    const SizedBox(height: 24),
                    const Divider(),
                    const SizedBox(height: 14),
                    const Text('Demo access',
                        style: TextStyle(fontWeight: FontWeight.w700)),
                    const SizedBox(height: 4),
                    const Text(
                        'Password: Nascon@26\nadmin · organizer · manager · player · viewer'),
                  ]),
            ),
          ),
        ),
      ]),
    );
  }

  Future<void> _login() async {
    setState(() {
      busy = true;
      error = null;
    });
    final e = await widget.app.login(emailC.text, passC.text);
    if (mounted)
      setState(() {
        busy = false;
        error = e;
      });
  }
}

Future<void> showServerDialog(BuildContext context, AppState app) async {
  final c = TextEditingController(text: app.baseUrl);
  await showDialog(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('Server address'),
      content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
                controller: c,
                decoration: const InputDecoration(labelText: 'http://IP:8080')),
            const SizedBox(height: 8),
            const Text(
                'Emulator: http://10.0.2.2:8080\nPhone: http://<laptop LAN IP>:8080'),
          ]),
      actions: [
        TextButton(
            onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
        FilledButton(
          onPressed: () async {
            await app.setBaseUrl(c.text);
            if (ctx.mounted) Navigator.pop(ctx);
          },
          child: const Text('Save'),
        ),
      ],
    ),
  );
}

class HomePage extends StatefulWidget {
  final AppState app;
  const HomePage({super.key, required this.app});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  int tab = 0;

  @override
  void initState() {
    super.initState();
    widget.app.refresh();
  }

  @override
  Widget build(BuildContext context) {
    final app = widget.app;
    final pages = <Widget>[
      RoleOverviewPage(app: app),
      FixturesPage(app: app),
      if (app.role != 'viewer') StandingsPage(app: app),
      if (app.role == 'super_admin' || app.role == 'club_manager')
        StorePage(app: app),
      if (app.role != 'viewer') ClubPage(app: app),
    ];
    final titles = <String>[
      roleHomeTitle(app.role),
      'Fixtures',
      if (app.role != 'viewer') 'Standings',
      if (app.role == 'super_admin' || app.role == 'club_manager') 'Store',
      if (app.role != 'viewer') 'Club',
    ];
    if (tab >= pages.length) tab = 0;
    return Scaffold(
      appBar: AppBar(
        title: Text(titles[tab]),
        actions: [
          Tooltip(
            message: '${app.email}\n${roleSummary(app.role)}',
            child: Container(
              constraints: const BoxConstraints(maxWidth: 150),
              margin: const EdgeInsets.only(right: 4),
              padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
              decoration: BoxDecoration(
                  color: const Color(0xFFEAF3EC),
                  borderRadius: BorderRadius.circular(6)),
              child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Text(roleLabel(app.role),
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                            fontSize: 11, fontWeight: FontWeight.w700)),
                    if (app.email.isNotEmpty)
                      Text(app.email,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(fontSize: 9)),
                  ]),
            ),
          ),
          IconButton(
              tooltip: 'Refresh',
              icon: const Icon(Icons.refresh),
              onPressed: app.refresh),
          PopupMenuButton<String>(
            tooltip: 'More',
            onSelected: (v) async {
              if (v == 'server') await showServerDialog(context, app);
              if (v == 'logout') {
                final ok = await confirm(context, 'Log out?',
                    'Pending offline actions stay on this device.');
                if (ok) app.logout();
              }
            },
            itemBuilder: (_) => const [
              PopupMenuItem(value: 'server', child: Text('Server settings')),
              PopupMenuItem(
                  value: 'logout', child: Text('Switch account / log out')),
            ],
          ),
        ],
      ),
      body:
          Column(children: [SyncBanner(app: app), Expanded(child: pages[tab])]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: tab,
        onDestinationSelected: (i) => setState(() => tab = i),
        destinations: [
          const NavigationDestination(
              icon: Icon(Icons.dashboard_outlined), label: 'Home'),
          const NavigationDestination(
              icon: Icon(Icons.event), label: 'Fixtures'),
          if (app.role != 'viewer')
            const NavigationDestination(
                icon: Icon(Icons.leaderboard), label: 'Standings'),
          if (app.role == 'super_admin' || app.role == 'club_manager')
            const NavigationDestination(
                icon: Icon(Icons.storefront), label: 'Store'),
          if (app.role != 'viewer')
            const NavigationDestination(
                icon: Icon(Icons.groups), label: 'Club'),
        ],
      ),
    );
  }
}

String roleHomeTitle(String role) => switch (role) {
      'super_admin' => 'Admin centre',
      'league_organizer' => 'League desk',
      'club_manager' => 'Club desk',
      'player' => 'Player home',
      'viewer' => 'League overview',
      _ => 'Home',
    };

class RoleOverviewPage extends StatelessWidget {
  final AppState app;
  const RoleOverviewPage({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final role = app.role;
    final d = app.data;
    final matches = (d?['matches'] as List?)?.length ?? 0;
    final players = ((d?['club']?['players'] as List?) ?? [])
        .where((p) => p is Map && p['squad_id'] == d?['club']?['club_id'])
        .length;
    final (
      String eyebrow,
      String title,
      String subtitle,
      IconData icon
    ) content = switch (role) {
      'super_admin' => (
          'Platform control',
          'Everything in one place',
          'Monitor leagues, clubs, commerce and access across the platform.',
          Icons.admin_panel_settings_outlined
        ),
      'league_organizer' => (
          'Competition operations',
          'Run matchday with confidence',
          'Review fixtures, standings and scoring activity for your league.',
          Icons.event_available
        ),
      'club_manager' => (
          'Club operations',
          'Keep your club moving',
          'Manage the squad, review fixtures and order official merchandise.',
          Icons.groups_outlined
        ),
      'player' => (
          'Your season',
          'Follow your club',
          'Stay close to fixtures, results and your current squad profile.',
          Icons.sports_soccer
        ),
      _ => (
          'Read-only access',
          'Follow the competition',
          'View fixtures and league progress without making changes.',
          Icons.visibility_outlined
        ),
    };
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 20, 20, 28),
      children: [
        Container(
          padding: const EdgeInsets.fromLTRB(20, 20, 20, 22),
          decoration: BoxDecoration(
            color: const Color(0xFFEAF3EC),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: const Color(0xFFD5E5D8)),
          ),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Container(
              width: 44,
              height: 44,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                  color: const Color(0xFF17634B),
                  borderRadius: BorderRadius.circular(10)),
              child: Icon(content.$4, color: Colors.white, size: 24),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(content.$1.toUpperCase(),
                        style: const TextStyle(
                            fontSize: 10,
                            letterSpacing: 1.5,
                            fontWeight: FontWeight.w800,
                            color: Color(0xFF365341))),
                    const SizedBox(height: 6),
                    Text(content.$2,
                        style: Theme.of(context).textTheme.titleLarge),
                    const SizedBox(height: 5),
                    Text(content.$3),
                  ]),
            ),
          ]),
        ),
        const SizedBox(height: 12),
        Card(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 14),
            child: Row(children: [
              const Icon(Icons.verified_user_outlined,
                  size: 20, color: Color(0xFF17634B)),
              const SizedBox(width: 10),
              Expanded(
                child: Text(roleLabel(role),
                    style: const TextStyle(fontWeight: FontWeight.w700)),
              ),
              Text('Active session',
                  style: Theme.of(context).textTheme.bodySmall),
            ]),
          ),
        ),
        const SizedBox(height: 18),
        Row(children: [
          Expanded(child: _overviewMetric(context, '$matches', 'Fixtures')),
          const SizedBox(width: 10),
          Expanded(
              child: _overviewMetric(context, '$players', 'Squad players')),
        ]),
        const SizedBox(height: 20),
        Text('Quick access', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        _overviewAction(context, Icons.event_outlined, 'Fixtures',
            'See upcoming and completed matches'),
        if (role != 'viewer')
          _overviewAction(context, Icons.leaderboard_outlined, 'Standings',
              'Track qualification and league position'),
        if (role == 'club_manager' || role == 'super_admin')
          _overviewAction(context, Icons.storefront_outlined, 'Club shop',
              'Review sizes, stock and checkout'),
        if (role == 'club_manager' || role == 'player')
          _overviewAction(
              context,
              Icons.groups_outlined,
              'Club profile',
              role == 'player'
                  ? 'View your club and squad'
                  : 'Manage your squad'),
      ],
    );
  }

  Widget _overviewMetric(BuildContext context, String value, String label) =>
      Card(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 14, 16, 16),
          child:
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(value, style: Theme.of(context).textTheme.headlineSmall),
            const SizedBox(height: 3),
            Text(label, style: Theme.of(context).textTheme.bodySmall),
          ]),
        ),
      );

  Widget _overviewAction(
          BuildContext context, IconData icon, String title, String subtitle) =>
      Card(
        margin: const EdgeInsets.only(bottom: 8),
        child: ListTile(
          minVerticalPadding: 10,
          leading: Icon(icon, color: Theme.of(context).colorScheme.primary),
          title:
              Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
          subtitle: Text(subtitle),
          trailing: const Icon(Icons.chevron_right),
        ),
      );
}

Future<bool> confirm(BuildContext context, String title, String body) async {
  final r = await showDialog<bool>(
    context: context,
    builder: (ctx) =>
        AlertDialog(title: Text(title), content: Text(body), actions: [
      TextButton(
          onPressed: () => Navigator.pop(ctx, false),
          child: const Text('Cancel')),
      FilledButton(
          onPressed: () => Navigator.pop(ctx, true),
          child: const Text('Confirm')),
    ]),
  );
  return r ?? false;
}

void toast(BuildContext context, String msg, {bool error = false}) {
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(
    content: Row(children: [
      Icon(error ? Icons.error_outline : Icons.check_circle,
          color: Colors.white),
      const SizedBox(width: 8),
      Expanded(child: Text(msg)),
    ]),
    backgroundColor: error ? Colors.red.shade700 : Colors.green.shade700,
  ));
}

Widget emptyState(String text, IconData icon) => Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 48, color: Colors.grey),
          const SizedBox(height: 12),
          Text(text,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 16)),
        ]),
      ),
    );

String statusLabel(String s) =>
    const {
      'upcoming': 'Upcoming',
      'in_progress': 'LIVE',
      'completed': 'Full time',
    }[s] ??
    s;

Widget statusChip(String s) {
  final live = s == 'in_progress';
  final color = live ? const Color(0xFFA52E2E) : const Color(0xFF52685D);
  return Container(
    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
    decoration: BoxDecoration(
        color: live ? const Color(0xFFFCECE8) : const Color(0xFFF0F3ED),
        borderRadius: BorderRadius.circular(6)),
    child: Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(
          live
              ? Icons.circle
              : (s == 'completed'
                  ? Icons.check_circle_outline
                  : Icons.schedule),
          size: 12,
          color: color),
      const SizedBox(width: 6),
      Text(statusLabel(s),
          style: TextStyle(
              fontSize: 11, fontWeight: FontWeight.w700, color: color))
    ]),
  );
}

Widget sectionIntro(String eyebrow, String title, String subtitle) => Padding(
      padding: const EdgeInsets.only(top: 8, bottom: 20),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(eyebrow.toUpperCase(),
            style: const TextStyle(
                fontSize: 10,
                letterSpacing: 1.8,
                fontWeight: FontWeight.w800,
                color: Color(0xFF61776A))),
        const SizedBox(height: 8),
        Text(title,
            style: const TextStyle(
                fontSize: 25,
                fontWeight: FontWeight.w800,
                letterSpacing: -0.8,
                color: Color(0xFF172B25))),
        const SizedBox(height: 6),
        Text(subtitle,
            style: const TextStyle(fontSize: 13, color: Color(0xFF65746B))),
      ]),
    );

Widget teamMark(String name) {
  final words = name.trim().split(RegExp(r'\s+'));
  final letters =
      words.where((w) => w.isNotEmpty).take(2).map((w) => w[0]).join();
  return Container(
      width: 36,
      height: 40,
      alignment: Alignment.center,
      decoration: BoxDecoration(
          color: const Color(0xFFEDF1E8),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFFDCE3D5))),
      child: Text(letters,
          style: const TextStyle(
              fontSize: 11,
              fontWeight: FontWeight.w800,
              color: Color(0xFF365341))));
}

class JerseyArtwork extends StatelessWidget {
  const JerseyArtwork({super.key});
  @override
  Widget build(BuildContext context) => Container(
        height: 210,
        width: double.infinity,
        margin: const EdgeInsets.only(bottom: 20),
        decoration: BoxDecoration(
            color: const Color(0xFFEDF0E6),
            borderRadius: BorderRadius.circular(12)),
        child: Stack(children: [
          const Positioned(
              left: 16,
              top: 16,
              child: Text('THE CLUB COLLECTION',
                  style: TextStyle(
                      fontSize: 10,
                      letterSpacing: 1.5,
                      fontWeight: FontWeight.w700,
                      color: Color(0xFF52644E)))),
          Center(
              child: Semantics(
                  label: 'Illustration of a green club jersey',
                  image: true,
                  child: CustomPaint(
                      size: const Size(170, 160),
                      painter: JerseyPainter(
                          color: Theme.of(context).colorScheme.primary)))),
          const Positioned(
              right: 16,
              bottom: 12,
              child: Text('SEASON 2026',
                  style: TextStyle(
                      fontSize: 10,
                      letterSpacing: 1.4,
                      color: Color(0xFF52644E)))),
        ]),
      );
}

class JerseyPainter extends CustomPainter {
  final Color color;
  JerseyPainter({required this.color});
  @override
  void paint(Canvas canvas, Size size) {
    canvas.save();
    canvas.scale(size.width / 170, size.height / 160);
    final path = Path()
      ..moveTo(52, 16)
      ..lineTo(68, 10)
      ..quadraticBezierTo(85, 26, 102, 10)
      ..lineTo(118, 16)
      ..lineTo(156, 45)
      ..lineTo(135, 72)
      ..lineTo(119, 61)
      ..lineTo(123, 148)
      ..quadraticBezierTo(85, 157, 47, 148)
      ..lineTo(51, 61)
      ..lineTo(35, 72)
      ..lineTo(14, 45)
      ..close();
    canvas.drawShadow(path, Colors.black.withOpacity(0.22), 6, true);
    canvas.drawPath(path, Paint()..color = color);
    canvas.save();
    canvas.clipPath(path);
    for (double x = 50; x < 130; x += 20) {
      canvas.drawRect(Rect.fromLTWH(x, 20, 8, 140),
          Paint()..color = Colors.white.withOpacity(0.09));
    }
    canvas.drawLine(
        const Offset(47, 141),
        const Offset(123, 141),
        Paint()
          ..color = const Color(0xFFD2D7AE)
          ..strokeWidth = 4);
    canvas.restore();
    canvas.drawArc(
        const Rect.fromLTWH(67, -2, 36, 31),
        0,
        3.14,
        false,
        Paint()
          ..color = const Color(0xFFD2D7AE)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 5);
    canvas.drawCircle(
        const Offset(105, 46), 7, Paint()..color = const Color(0xFFE7E4C7));
    final text = TextPainter(
        text: const TextSpan(
            text: 'ALPHA',
            style: TextStyle(
                color: Colors.white,
                fontSize: 17,
                fontWeight: FontWeight.w800,
                letterSpacing: 2)),
        textDirection: TextDirection.ltr)
      ..layout();
    text.paint(canvas, Offset((170 - text.width) / 2, 80));
    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant JerseyPainter oldDelegate) =>
      oldDelegate.color != color;
}

// ---------------- Fixtures ----------------
class FixturesPage extends StatelessWidget {
  final AppState app;
  const FixturesPage({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final d = app.data;
    if (d == null) {
      return app.online
          ? const Center(child: CircularProgressIndicator())
          : emptyState(
              'No saved fixtures yet. Connect to the internet once to load them.',
              Icons.cloud_off);
    }
    final matches = (d['matches'] as List).cast<Map>();
    if (matches.isEmpty) return emptyState('No matches yet', Icons.event_busy);
    return RefreshIndicator(
      onRefresh: app.refresh,
      child: ListView.separated(
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
        itemCount: matches.length + 1,
        separatorBuilder: (_, __) => const SizedBox(height: 12),
        itemBuilder: (_, i) {
          if (i == 0) {
            return sectionIntro('Match centre', d['league']['name'] as String,
                'Every fixture. Every result. One place.');
          }
          final m = matches[i - 1];
          final ko = DateTime.tryParse(m['kickoff'] as String)?.toLocal();
          return Card(
            child: InkWell(
              borderRadius: BorderRadius.circular(12),
              onTap: () => Navigator.push(
                  context,
                  MaterialPageRoute(
                      builder: (_) => MatchPage(
                          app: app, match: Map<String, dynamic>.from(m)))),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(18, 14, 18, 18),
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                          crossAxisAlignment: CrossAxisAlignment.center,
                          children: [
                            statusChip(m['status'] as String),
                            const Spacer(),
                            if (ko != null)
                              Text(
                                  '${ko.day}/${ko.month}  ${ko.hour.toString().padLeft(2, '0')}:${ko.minute.toString().padLeft(2, '0')}'),
                          ]),
                      const SizedBox(height: 8),
                      Row(children: [
                        Expanded(
                            child: Column(children: [
                          teamMark(m['home_name'] as String),
                          const SizedBox(height: 9),
                          Text(m['home_name'] as String,
                              textAlign: TextAlign.center,
                              style: const TextStyle(
                                  fontSize: 13, fontWeight: FontWeight.w700))
                        ])),
                        Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 18),
                            child: Text(
                                m['status'] == 'upcoming'
                                    ? 'VS'
                                    : '${m['home_score']} – ${m['away_score']}',
                                style: const TextStyle(
                                    fontSize: 25,
                                    fontWeight: FontWeight.w800,
                                    letterSpacing: -1))),
                        Expanded(
                            child: Column(children: [
                          teamMark(m['away_name'] as String),
                          const SizedBox(height: 9),
                          Text(m['away_name'] as String,
                              textAlign: TextAlign.center,
                              style: const TextStyle(
                                  fontSize: 13, fontWeight: FontWeight.w700))
                        ])),
                      ]),
                    ]),
              ),
            ),
          );
        },
      ),
    );
  }
}

// ---------------- Match detail + scoring ----------------
class MatchPage extends StatefulWidget {
  final AppState app;
  final Map<String, dynamic> match;
  const MatchPage({super.key, required this.app, required this.match});
  @override
  State<MatchPage> createState() => _MatchPageState();
}

class _MatchPageState extends State<MatchPage> {
  Map<String, dynamic>? score;
  Timer? poll;
  String get mid => widget.match['match_id'] as String;

  @override
  void initState() {
    super.initState();
    score = widget.app.cached('score:$mid');
    load();
    poll = Timer.periodic(const Duration(seconds: 3), (_) => load());
  }

  @override
  void dispose() {
    poll?.cancel();
    super.dispose();
  }

  Future<void> load() async {
    final r = await widget.app.get('/audit/v1/score/$mid');
    if (r.status == 200 && mounted) {
      setState(() => score = Map<String, dynamic>.from(r.body));
      widget.app.putCache('score:$mid', r.body);
    }
  }

  int get version =>
      (score?['version'] as int?) ?? (widget.match['version'] as int? ?? 0);

  Future<void> sendEvent(
      String type, Map<String, dynamic> payload, String label) async {
    final app = widget.app;
    final body = {
      'event_id': 'app-${DateTime.now().microsecondsSinceEpoch}',
      'match_id': mid,
      'type': type,
      'payload': payload,
    };
    if (!app.online) {
      await app.enqueue('score_event', version, body, label);
      if (mounted)
        toast(context, 'Offline: "$label" saved and will sync automatically.');
      return;
    }
    final r = await app.post('/audit/v1/events', body);
    if (r.ok) {
      if (mounted) toast(context, '$label recorded');
      await load();
    } else if (r.status == 0) {
      await app.enqueue('score_event', version, body, label);
      if (mounted)
        toast(context, 'Connection lost: "$label" saved for later sync.');
    } else {
      if (mounted) toast(context, r.message, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final app = widget.app;
    final m = widget.match;
    final names = Map<String, dynamic>.from(app.data?['teams'] ?? {});
    String nm(String? id) => (names[id] ?? id ?? '') as String;
    final status = (score?['status'] ?? m['status']) as String;
    final hs = score?['home']?['score'] ?? m['home_score'];
    final as_ = score?['away']?['score'] ?? m['away_score'];
    final events =
        ((score?['events'] as List?) ?? []).cast<Map>().reversed.toList();
    final canScore =
        app.role == 'league_organizer' || app.role == 'super_admin';
    final evTypes = ((app.data?['league']?['scoring_events'] as List?) ?? [])
        .cast<String>();
    final store = app.data?['store'];
    final pinned = store != null && store['pinned_on_match'] == mid;

    return Scaffold(
      appBar: AppBar(title: const Text('Match')),
      body: Column(children: [
        SyncBanner(app: app),
        Expanded(
          child: ListView(
              padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
              children: [
                Card(
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(16, 14, 16, 18),
                    child: Column(children: [
                      Align(
                          alignment: Alignment.centerLeft,
                          child: statusChip(status)),
                      const SizedBox(height: 18),
                      Semantics(
                        label: '${nm(m['home'])} $hs, ${nm(m['away'])} $as_',
                        child: Row(children: [
                          Expanded(
                              child: Column(children: [
                            teamMark(nm(m['home'])),
                            const SizedBox(height: 8),
                            Text(nm(m['home']),
                                textAlign: TextAlign.center,
                                style: const TextStyle(
                                    fontWeight: FontWeight.w700)),
                          ])),
                          Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 12),
                            child: Text('$hs – $as_',
                                style:
                                    Theme.of(context).textTheme.headlineSmall),
                          ),
                          Expanded(
                              child: Column(children: [
                            teamMark(nm(m['away'])),
                            const SizedBox(height: 8),
                            Text(nm(m['away']),
                                textAlign: TextAlign.center,
                                style: const TextStyle(
                                    fontWeight: FontWeight.w700)),
                          ])),
                        ]),
                      ),
                    ]),
                  ),
                ),
                const SizedBox(height: 8),
                const Center(
                    child: Text('Updates live every few seconds',
                        style: TextStyle(color: Colors.black54))),
                if (pinned) ...[
                  const SizedBox(height: 16),
                  Card(
                    color: Theme.of(context).colorScheme.secondaryContainer,
                    child: ListTile(
                      leading: const Icon(Icons.checkroom, size: 32),
                      title: Text(store['name'] as String),
                      subtitle: Text(
                          'Featured product · ${pkr(store['variants'][0]['price_minor'] as num)}'),
                      trailing: const Icon(Icons.chevron_right),
                      onTap: () => Navigator.push(
                          context,
                          MaterialPageRoute(
                              builder: (_) => Scaffold(
                                    appBar: AppBar(title: const Text('Store')),
                                    body: Column(children: [
                                      SyncBanner(app: app),
                                      Expanded(child: StorePage(app: app))
                                    ]),
                                  ))),
                    ),
                  ),
                ],
                if (canScore) ...[
                  const SizedBox(height: 20),
                  Text('Scoring (Organizer)',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  Wrap(spacing: 8, runSpacing: 8, children: [
                    if (status == 'upcoming')
                      FilledButton.icon(
                        icon: const Icon(Icons.play_arrow),
                        label: const Text('Start match'),
                        onPressed: () =>
                            sendEvent('match_start', {}, 'Match started'),
                      ),
                    if (status == 'in_progress') ...[
                      for (final side in ['home', 'away'])
                        for (final t in evTypes)
                          OutlinedButton(
                            style: OutlinedButton.styleFrom(
                                minimumSize: const Size(48, 48)),
                            onPressed: () => sendEvent(
                                'score',
                                {'team_id': m[side], 'event_type': t},
                                '$t for ${nm(m[side])}'),
                            child: Text('+ $t · ${nm(m[side])}'),
                          ),
                      FilledButton.tonalIcon(
                        icon: const Icon(Icons.stop),
                        label: const Text('End match'),
                        onPressed: () async {
                          if (await confirm(context, 'End this match?',
                              'The final score will count in the standings.')) {
                            sendEvent('match_end', {}, 'Match ended');
                          }
                        },
                      ),
                    ],
                  ]),
                ],
                const SizedBox(height: 20),
                Text('Timeline',
                    style: Theme.of(context).textTheme.titleMedium),
                if (events.isEmpty)
                  const Padding(
                      padding: EdgeInsets.all(16),
                      child: Text('No events yet')),
                for (final e in events)
                  ListTile(
                    leading: Icon(e['type'] == 'compensation'
                        ? Icons.undo
                        : (e['type'] == 'score'
                            ? Icons.sports_soccer
                            : Icons.info_outline)),
                    title: Text(
                      e['type'] == 'score'
                          ? '${e['payload']['event_type']} · ${nm(e['payload']['team_id'] as String?)}'
                          : (e['type'] == 'compensation'
                              ? 'Correction: ${e['payload']['reason']}'
                              : e['type'] as String),
                      style: TextStyle(
                          decoration: e['compensated'] == true
                              ? TextDecoration.lineThrough
                              : null),
                    ),
                    subtitle: Text(e['compensated'] == true
                        ? 'Cancelled by correction'
                        : '#${e['sequence']}'),
                    trailing: (canScore &&
                            e['type'] == 'score' &&
                            e['compensated'] != true)
                        ? IconButton(
                            tooltip: 'Cancel this event (correction)',
                            icon: const Icon(Icons.undo),
                            onPressed: () async {
                              if (await confirm(context, 'Cancel this score?',
                                  'A correction event will be added. History is kept.')) {
                                sendEvent(
                                    'compensation',
                                    {
                                      'compensates': e['event_id'],
                                      'reason': 'Disallowed'
                                    },
                                    'Correction');
                              }
                            },
                          )
                        : null,
                  ),
              ]),
        ),
      ]),
    );
  }
}

// ---------------- Standings ----------------
class StandingsPage extends StatelessWidget {
  final AppState app;
  const StandingsPage({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final s = app.cached('standings');
    if (s == null)
      return emptyState(
          'No standings yet. Connect once to load them.', Icons.leaderboard);
    final names = Map<String, dynamic>.from(app.data?['teams'] ?? {});
    final table = (s['table'] as List).cast<Map>();
    if (table.isEmpty)
      return emptyState('No teams in this league yet', Icons.leaderboard);
    return RefreshIndicator(
      onRefresh: app.refresh,
      child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
          children: [
            sectionIntro(
                'Competition table',
                app.data?['league']?['name'] ?? '',
                'Top ${s['qualification_spots']} teams qualify for the next stage.'),
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Card(
                clipBehavior: Clip.antiAlias,
                child: DataTable(
                  columnSpacing: 14,
                  columns: const [
                    DataColumn(label: Text('#')),
                    DataColumn(label: Text('Team')),
                    DataColumn(label: Text('P'), numeric: true),
                    DataColumn(label: Text('W'), numeric: true),
                    DataColumn(label: Text('D'), numeric: true),
                    DataColumn(label: Text('L'), numeric: true),
                    DataColumn(label: Text('GD'), numeric: true),
                    DataColumn(label: Text('Pts'), numeric: true),
                    DataColumn(label: Text('Status')),
                  ],
                  rows: [
                    for (final r in table)
                      DataRow(cells: [
                        DataCell(Text('${r['rank']}')),
                        DataCell(Text(
                            (names[r['team_id']] ?? r['team_id']) as String)),
                        DataCell(Text('${r['played']}')),
                        DataCell(Text('${r['won']}')),
                        DataCell(Text('${r['drawn']}')),
                        DataCell(Text('${r['lost']}')),
                        DataCell(Text('${r['diff']}')),
                        DataCell(Text('${r['points']}',
                            style:
                                const TextStyle(fontWeight: FontWeight.bold))),
                        DataCell(qualChip(r['status'] as String)),
                      ]),
                  ],
                ),
              ),
            ),
          ]),
    );
  }

  // Meaning shown by icon + text, never colour alone.
  Widget qualChip(String s) {
    final m = {
      'clinched': (Icons.verified, 'Qualified', Colors.green),
      'eliminated': (Icons.cancel, 'Out', Colors.red),
      'still_possible': (Icons.hourglass_bottom, 'In race', Colors.orange),
    }[s]!;
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(m.$1, size: 16, color: m.$3),
      const SizedBox(width: 4),
      Text(m.$2),
    ]);
  }
}

// ---------------- Store ----------------
class StorePage extends StatefulWidget {
  final AppState app;
  const StorePage({super.key, required this.app});
  @override
  State<StorePage> createState() => _StorePageState();
}

class _StorePageState extends State<StorePage> {
  String? variant;
  int qty = 1;
  final codeC = TextEditingController();
  String token = 'tok_success';
  bool busy = false;

  @override
  Widget build(BuildContext context) {
    final app = widget.app;
    final st = app.data?['store'];
    if (st == null) return emptyState('Store not loaded yet', Icons.storefront);
    final vars = (st['variants'] as List).cast<Map>();
    variant ??= vars.isNotEmpty ? vars.first['variant_id'] as String : null;
    final sel = vars.firstWhere((v) => v['variant_id'] == variant,
        orElse: () => vars.first);
    final price = sel['price_minor'] as int;
    final subtotal = price * qty;
    final discount =
        codeC.text.trim() == 'WELCOME10' ? subtotal * 10 ~/ 100 : 0;
    final stock = sel['stock'] as int;

    return ListView(
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
        children: [
          sectionIntro('Club shop', st['name'] as String,
              'Official kit and essentials for the season.'),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(18),
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const JerseyArtwork(),
                    const Text('OFFICIAL MERCHANDISE',
                        style: TextStyle(
                            fontSize: 10,
                            letterSpacing: 1.4,
                            fontWeight: FontWeight.w700,
                            color: Color(0xFF61776A))),
                    const SizedBox(height: 8),
                    Row(children: [
                      Expanded(
                          child: Text(st['name'] as String,
                              style: Theme.of(context).textTheme.titleLarge)),
                      Text(pkr(price),
                          style: Theme.of(context).textTheme.titleMedium),
                    ]),
                    const SizedBox(height: 8),
                    Row(children: [
                      Icon(stock > 0 ? Icons.inventory_2_outlined : Icons.block,
                          size: 16,
                          color:
                              stock > 0 ? const Color(0xFF17634B) : Colors.red),
                      const SizedBox(width: 6),
                      Text(stock > 0 ? '$stock available' : 'Out of stock',
                          style: TextStyle(
                              color: stock > 0
                                  ? const Color(0xFF365341)
                                  : Colors.red,
                              fontWeight: FontWeight.w600)),
                    ]),
                  ]),
            ),
          ),
          const SizedBox(height: 12),
          const Text('Choose your size',
              style: TextStyle(fontWeight: FontWeight.w700)),
          const SizedBox(height: 6),
          Wrap(spacing: 8, children: [
            for (final v in vars)
              ChoiceChip(
                label: Text(
                    '${v['size']} · ${v['stock'] == 0 ? 'sold out' : '${v['stock']} left'}'),
                selected: variant == v['variant_id'],
                onSelected: (_) =>
                    setState(() => variant = v['variant_id'] as String),
              ),
          ]),
          const SizedBox(height: 12),
          Row(children: [
            const Text('Quantity'),
            const Spacer(),
            IconButton(
                tooltip: 'Decrease quantity',
                onPressed: qty > 1 ? () => setState(() => qty--) : null,
                icon: const Icon(Icons.remove_circle_outline)),
            Text('$qty', style: const TextStyle(fontSize: 18)),
            IconButton(
                tooltip: 'Increase quantity',
                onPressed: qty < 10 ? () => setState(() => qty++) : null,
                icon: const Icon(Icons.add_circle_outline)),
          ]),
          TextField(
            controller: codeC,
            onChanged: (_) => setState(() {}),
            decoration: InputDecoration(
                labelText: 'Discount code (optional)',
                hintText: st['discount_hint'] as String?,
                border: const OutlineInputBorder()),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            value: token,
            decoration: const InputDecoration(
                labelText: 'Test card (mock gateway)',
                border: OutlineInputBorder()),
            items: const [
              DropdownMenuItem(
                  value: 'tok_success', child: Text('Card that works')),
              DropdownMenuItem(
                  value: 'tok_declined', child: Text('Card that is declined')),
              DropdownMenuItem(
                  value: 'tok_capture_fail',
                  child: Text('Card that fails at capture')),
            ],
            onChanged: (v) => setState(() => token = v!),
          ),
          const SizedBox(height: 16),
          _row('Subtotal', pkr(subtotal)),
          if (discount > 0) _row('Discount', '− ${pkr(discount)}'),
          _row('Total', pkr(subtotal - discount), bold: true),
          const SizedBox(height: 16),
          SizedBox(
            height: 56,
            child: FilledButton.icon(
              icon: busy
                  ? const SizedBox.shrink()
                  : const Icon(Icons.shopping_cart_checkout),
              label: busy
                  ? const CircularProgressIndicator()
                  : Text(stock == 0 ? 'Sold out' : 'Confirm checkout',
                      style: const TextStyle(fontSize: 18)),
              onPressed: (busy || stock == 0)
                  ? null
                  : () async {
                      if (!app.online) {
                        toast(context,
                            'You are offline. Purchases need a connection.',
                            error: true);
                        return;
                      }
                      final ok = await confirm(context, 'Confirm purchase',
                          'Pay ${pkr(subtotal - discount)} for $qty × size ${sel['size']}?');
                      if (!ok) return;
                      setState(() => busy = true);
                      final r = await app.post('/audit/v1/checkout', {
                        'order_id':
                            'ord-${DateTime.now().microsecondsSinceEpoch}',
                        'customer_id': app.email,
                        'items': [
                          {'variant_id': variant, 'quantity': qty}
                        ],
                        'discount_code': codeC.text.trim().isEmpty
                            ? null
                            : codeC.text.trim(),
                        'payment': {'token': token},
                      });
                      setState(() => busy = false);
                      await app.refresh();
                      if (!mounted) return;
                      if (r.ok) {
                        Navigator.push(
                            context,
                            MaterialPageRoute(
                                builder: (_) => ReceiptPage(
                                    order: Map<String, dynamic>.from(r.body))));
                      } else {
                        toast(context, r.message, error: true);
                      }
                    },
            ),
          ),
        ]);
  }

  Widget _row(String a, String b, {bool bold = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(children: [
          Text(a, style: TextStyle(fontWeight: bold ? FontWeight.bold : null)),
          const Spacer(),
          Text(b,
              style: TextStyle(
                  fontWeight: bold ? FontWeight.bold : null,
                  fontSize: bold ? 18 : 16)),
        ]),
      );
}

class ReceiptPage extends StatelessWidget {
  final Map<String, dynamic> order;
  const ReceiptPage({super.key, required this.order});

  @override
  Widget build(BuildContext context) {
    final p = order['receipt']['payload'];
    final pdfBytes =
        base64Decode(order['receipt']['pdf_base64'] as String).length;
    return Scaffold(
      appBar: AppBar(title: const Text('Receipt')),
      body: ListView(padding: const EdgeInsets.all(20), children: [
        const Icon(Icons.check_circle,
            color: Colors.green, size: 72, semanticLabel: 'Payment successful'),
        const Center(
            child: Text('Payment successful',
                style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold))),
        const SizedBox(height: 16),
        ListTile(
            title: const Text('Order'),
            trailing: Text(p['order_id'] as String)),
        for (final it in (p['items'] as List))
          ListTile(
              title: Text('${it['variant_id']} × ${it['quantity']}'),
              trailing: Text(pkr(it['unit_price_minor'] as num))),
        ListTile(
            title: const Text('Discount'),
            trailing: Text(pkr(p['discount_minor'] as num))),
        ListTile(
            title: const Text('Total',
                style: TextStyle(fontWeight: FontWeight.bold)),
            trailing: Text(pkr(p['total_minor'] as num),
                style: const TextStyle(
                    fontWeight: FontWeight.bold, fontSize: 18))),
        ListTile(
            title: const Text('Paid at'),
            trailing: Text(p['paid_at'] as String)),
        const Divider(),
        ListTile(
          leading: const Icon(Icons.picture_as_pdf),
          title: const Text('PDF receipt generated on server'),
          subtitle: Text(
              '$pdfBytes bytes · signed ${(order['receipt']['signature'] as String).substring(0, 12)}…'),
        ),
        const SizedBox(height: 12),
        FilledButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Back to store')),
      ]),
    );
  }
}

// ---------------- Club + roster ----------------
class ClubPage extends StatelessWidget {
  final AppState app;
  const ClubPage({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final c = app.data?['club'];
    if (c == null)
      return emptyState('Club profile not loaded yet', Icons.groups);
    final players = (c['players'] as List).cast<Map>();
    final mine = players.where((p) => p['squad_id'] == c['club_id']).toList();
    final isManager = app.role == 'club_manager' || app.role == 'super_admin';
    return ListView(
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
        children: [
          sectionIntro('Club profile', c['name'] as String,
              'The people behind the badge.'),
          Row(children: [
            teamMark(c['name'] as String),
            const SizedBox(width: 12),
            Expanded(
                child: Text(c['history'] as String,
                    style: Theme.of(context).textTheme.bodyMedium)),
          ]),
          const SizedBox(height: 4),
          Row(children: [
            const Icon(Icons.stadium, size: 18),
            const SizedBox(width: 6),
            Expanded(child: Text(c['venue'] as String))
          ]),
          Row(children: [
            const Icon(Icons.alternate_email, size: 18),
            const SizedBox(width: 6),
            Text(c['social'] as String)
          ]),
          const SizedBox(height: 16),
          Text('Staff', style: Theme.of(context).textTheme.titleMedium),
          for (final s in (c['staff'] as List))
            ListTile(
                leading: const CircleAvatar(
                    backgroundColor: Color(0xFFEAF0E6),
                    child:
                        Icon(Icons.person_outline, color: Color(0xFF365341))),
                title: Text(s['name'] as String),
                subtitle: Text(s['role'] as String)),
          const SizedBox(height: 8),
          Text('Roster (${mine.length})',
              style: Theme.of(context).textTheme.titleMedium),
          if (mine.isEmpty)
            const Padding(
                padding: EdgeInsets.all(16),
                child: Text('No players in this squad yet')),
          for (final p in mine)
            ListTile(
              leading: CircleAvatar(
                  child: Text((p['position'] as String).toUpperCase())),
              title: Text(p['name'] as String),
              subtitle: Text((p['role'] as String? ?? '').replaceAll('_', ' ')),
              trailing: isManager
                  ? TextButton(
                      style:
                          TextButton.styleFrom(minimumSize: const Size(48, 48)),
                      onPressed: () => showRosterSheet(
                          context, app, Map<String, dynamic>.from(p)),
                      child: const Text('Manage'),
                    )
                  : null,
            ),
        ]);
  }
}

Future<void> showRosterSheet(
    BuildContext context, AppState app, Map<String, dynamic> p) async {
  final c = app.data!['club'];
  final other =
      (c['squads'] as List).firstWhere((s) => s['squad_id'] != c['club_id']);
  final today = DateTime.now().toIso8601String().substring(0, 10);
  final loanEnd = DateTime.now()
      .add(const Duration(days: 30))
      .toIso8601String()
      .substring(0, 10);

  Future<void> run(String type) async {
    final label = '${type[0].toUpperCase()}${type.substring(1)} ${p['name']}';
    final ok = await confirm(
        context,
        '$label?',
        type == 'release'
            ? 'The player will leave the league.'
            : 'Move to ${other['name']}${type == 'loan' ? ' until $loanEnd' : ''}.');
    if (!ok) return;
    final body = {
      'request_id': 'app-${DateTime.now().microsecondsSinceEpoch}',
      'league_id': c['roster_league_id'],
      'player_id': p['player_id'],
      'type': type,
      'from_squad_id': p['squad_id'],
      'to_squad_id': type == 'release' ? null : other['squad_id'],
      'effective_date': today,
      'loan_end_date': type == 'loan' ? loanEnd : null,
      'reason': 'Changed from mobile app',
      'requested_by': app.email,
    };
    final base = (p['version'] as int?) ?? 0;
    if (!app.online) {
      await app.enqueue('roster_transfer', base, body, label);
      if (context.mounted)
        toast(context, 'Offline: "$label" saved and will sync automatically.');
      return;
    }
    final r = await app.post('/audit/v1/roster/transfer', body);
    if (r.status == 0) {
      await app.enqueue('roster_transfer', base, body, label);
      if (context.mounted)
        toast(context, 'Connection lost: "$label" saved for later sync.');
    } else if (r.ok) {
      await app.refresh();
      if (context.mounted) toast(context, '$label done');
    } else if (context.mounted) {
      toast(context, r.message, error: true);
    }
  }

  await showModalBottomSheet(
    context: context,
    builder: (ctx) => SafeArea(
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        ListTile(
            title: Text(p['name'] as String,
                style: const TextStyle(fontWeight: FontWeight.bold))),
        ListTile(
            leading: const Icon(Icons.swap_horiz),
            title: Text('Transfer to ${other['name']}'),
            onTap: () {
              Navigator.pop(ctx);
              run('transfer');
            }),
        ListTile(
            leading: const Icon(Icons.schedule),
            title: Text('Loan to ${other['name']} (30 days)'),
            onTap: () {
              Navigator.pop(ctx);
              run('loan');
            }),
        ListTile(
            leading: const Icon(Icons.logout),
            title: const Text('Release player'),
            onTap: () {
              Navigator.pop(ctx);
              run('release');
            }),
        ListTile(
          leading: const Icon(Icons.history),
          title: const Text('View history'),
          onTap: () async {
            Navigator.pop(ctx);
            final r =
                await app.get('/audit/v1/roster/history/${p['player_id']}');
            if (!context.mounted) return;
            final h = r.ok ? (r.body['history'] as List) : [];
            showDialog(
              context: context,
              builder: (_) => AlertDialog(
                title: const Text('Roster history'),
                content: SizedBox(
                  width: double.maxFinite,
                  child: h.isEmpty
                      ? Text(r.ok
                          ? 'No changes yet'
                          : 'History needs a connection')
                      : ListView(shrinkWrap: true, children: [
                          for (final e in h)
                            ListTile(
                                title: Text(
                                    '${e['action']} · ${e['effective_date']}'),
                                subtitle: Text('by ${e['who']}: ${e['why']}')),
                        ]),
                ),
              ),
            );
          },
        ),
      ]),
    ),
  );
}

// ---------------- Pending queue + sync results ----------------
class QueuePage extends StatelessWidget {
  final AppState app;
  const QueuePage({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: app,
      builder: (context, _) {
        final q = app.queue;
        return Scaffold(
          appBar: AppBar(title: const Text('Sync queue')),
          body: Column(children: [
            SyncBanner(app: app),
            Padding(
              padding: const EdgeInsets.all(16),
              child: SizedBox(
                width: double.infinity,
                height: 52,
                child: FilledButton.icon(
                  icon: const Icon(Icons.sync),
                  label:
                      Text(app.online ? 'Sync now' : 'Waiting for connection…'),
                  onPressed: (!app.online || q.isEmpty || app.syncing)
                      ? null
                      : () => app.syncNow(),
                ),
              ),
            ),
            Expanded(
              child: ListView(children: [
                const ListTile(
                    title: Text('Pending',
                        style: TextStyle(fontWeight: FontWeight.bold))),
                if (q.isEmpty)
                  const ListTile(
                      leading: Icon(Icons.done_all),
                      title: Text('Nothing waiting to sync')),
                for (final a in q)
                  ListTile(
                    leading: const Icon(Icons.pending_actions),
                    title:
                        Text(a['label'] as String? ?? a['action_id'] as String),
                    subtitle: Text('Pending · saved ${a['queued_at']}'),
                  ),
                if (app.lastSyncResults.isNotEmpty) ...[
                  const Divider(),
                  const ListTile(
                      title: Text('Last sync results',
                          style: TextStyle(fontWeight: FontWeight.bold))),
                  for (final r in app.lastSyncResults)
                    ListTile(
                      leading: Icon(
                        r['status'] == 'applied'
                            ? Icons.check_circle
                            : (r['status'] == 'duplicate'
                                ? Icons.copy
                                : Icons.block),
                        color: r['status'] == 'applied'
                            ? Colors.green
                            : (r['status'] == 'duplicate'
                                ? Colors.grey
                                : Colors.red),
                      ),
                      title: Text(r['label'] as String),
                      subtitle: Text(r['status'] == 'applied'
                          ? (r['resolution'] == 'rebased_append'
                              ? 'Applied (merged with newer server data)'
                              : 'Applied')
                          : r['status'] == 'duplicate'
                              ? 'Already on the server, skipped'
                              : 'Rejected: ${explainCode(r['error_code'] as String?) ?? 'the server did not accept this change.'}'),
                    ),
                ],
              ]),
            ),
          ]),
        );
      },
    );
  }
}
