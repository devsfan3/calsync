// calbridge — a thin JSON bridge to macOS EventKit and Notification Centre.
//
// Lives inside CalSyncBridge.app so that macOS attributes the Calendar (TCC)
// permission to this bundle rather than to whatever launched it. Input and
// output are files rather than stdin/stdout because the app is started through
// LaunchServices, which leaves it no usable stdio — see the note further down.
//
// Subcommands (input JSON comes from --in, response goes to --out):
//   auth        request/report calendar access
//   version     report bundle version, build date and commit
//   calendars   list every calendar EventKit can see
//   events      {"calendarIds":[...], "days":N}
//   create      {"calendarId":..,"title":..,"start":..,"end":..,"allDay":Bool}
//   update      {"eventId":..,"title":..,"start":..,"end":..,"allDay":Bool}
//   delete      {"eventId":..}
//   notify      {"title":..,"body":..}
//
// Every response is a single JSON object: {"ok":true,...} or {"ok":false,"error":".."}

import Foundation
import EventKit
import UserNotifications
import AppKit

let store = EKEventStore()

// MARK: - I/O plumbing
//
// When this binary is exec'd from a shell, TCC blames whatever launched the
// shell (Terminal, an IDE, ...) instead of this bundle. Launching it through
// LaunchServices — `open -W -a CalSyncBridge.app --args ...` — makes it its own
// responsible process with its own Calendar permission entry. LaunchServices
// gives the app no usable stdio, so --in/--out name real files instead.

var inputPath: String? = nil
var outputPath: String? = nil

// MARK: - Output helpers

func emit(_ obj: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    if let path = outputPath {
        // Write to a sibling temp file and rename, so a reader polling for the
        // result never observes a half-written response.
        let tmp = path + ".partial"
        FileManager.default.createFile(atPath: tmp, contents: data)
        try? FileManager.default.removeItem(atPath: path)
        try? FileManager.default.moveItem(atPath: tmp, toPath: path)
    } else {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    }
    exit(obj["ok"] as? Bool == true ? 0 : 1)
}

func fail(_ message: String) -> Never {
    emit(["ok": false, "error": message])
}

let iso: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

func parseDate(_ s: Any?) -> Date? {
    guard let s = s as? String else { return nil }
    if let d = iso.date(from: s) { return d }
    // Tolerate fractional seconds.
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.date(from: s)
}

func readInputJSON() -> [String: Any] {
    let data: Data
    if let path = inputPath {
        guard let d = FileManager.default.contents(atPath: path) else {
            fail("could not read input file \(path)")
        }
        data = d
    } else {
        data = FileHandle.standardInput.readDataToEndOfFile()
    }
    guard !data.isEmpty,
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        fail("expected a JSON object as input")
    }
    return obj
}

// MARK: - Access

/// EventKit's completion handler fires on a background queue; block until it lands.
func requestAccess() -> (granted: Bool, error: String?) {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    var errText: String? = nil

    let handler: (Bool, Error?) -> Void = { ok, err in
        granted = ok
        if let err = err { errText = err.localizedDescription }
        sem.signal()
    }

    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents(completion: handler)
    } else {
        store.requestAccess(to: .event, completion: handler)
    }

    // Generous timeout: the very first call shows a system prompt the user must answer.
    if sem.wait(timeout: .now() + 120) == .timedOut {
        return (false, "timed out waiting for calendar permission")
    }
    return (granted, errText)
}

func requireAccess() {
    let (granted, err) = requestAccess()
    if !granted {
        fail("calendar access denied\(err.map { ": \($0)" } ?? ""). Grant it in System Settings > Privacy & Security > Calendars for CalSyncBridge.")
    }
}

// MARK: - Serialization

func describe(_ cal: EKCalendar) -> [String: Any] {
    return [
        "id": cal.calendarIdentifier,
        "title": cal.title,
        "source": cal.source?.title ?? "",
        "sourceType": sourceTypeName(cal.source?.sourceType),
        "writable": cal.allowsContentModifications,
        "subscribed": cal.isSubscribed,
        "immutable": cal.isImmutable,
    ]
}

func sourceTypeName(_ t: EKSourceType?) -> String {
    switch t {
    case .some(.local): return "local"
    case .some(.exchange): return "exchange"
    case .some(.calDAV): return "caldav"
    case .some(.mobileMe): return "mobileme"
    case .some(.subscribed): return "subscribed"
    case .some(.birthdays): return "birthdays"
    default: return "unknown"
    }
}

func describe(_ ev: EKEvent) -> [String: Any] {
    var out: [String: Any] = [
        // eventIdentifier is stable per event; for a repeating series every
        // occurrence shares it, so callers must pair it with "start".
        "id": ev.eventIdentifier ?? "",
        "externalId": ev.calendarItemExternalIdentifier ?? "",
        "title": ev.title ?? "",
        "allDay": ev.isAllDay,
        "calendarId": ev.calendar?.calendarIdentifier ?? "",
        "calendarTitle": ev.calendar?.title ?? "",
        "recurring": ev.hasRecurrenceRules,
        "status": statusName(ev.status),
        // Whether the event carries anything descriptive, reported as a flag
        // rather than as content so a mirror can be audited without the
        // private text ending up in a log or a JSON dump.
        "hasDetails": !(ev.notes ?? "").isEmpty
            || !(ev.location ?? "").isEmpty
            || ev.url != nil,
    ]
    if let s = ev.startDate { out["start"] = iso.string(from: s) }
    if let e = ev.endDate { out["end"] = iso.string(from: e) }
    if let loc = ev.location, !loc.isEmpty { out["location"] = loc }
    if let tz = ev.timeZone { out["timeZone"] = tz.identifier }
    return out
}

/// Exchange and iCloud both support busy, but a subscribed or local calendar
/// may not — setting an unsupported availability makes the save fail.
func markBusy(_ ev: EKEvent) {
    if let cal = ev.calendar, cal.supportedEventAvailabilities.contains(.busy) {
        ev.availability = .busy
    }
}

/// Clear every field that could describe what an event actually is.
///
/// A mirror exists to say "this time is taken" and nothing more. The title is
/// chosen deliberately at approval time; everything else is stripped here, on
/// both create and update, so no caller can leak detail onto the work calendar
/// by passing an extra field. A colleague with permission to see event details
/// must learn nothing beyond the fact that the time is busy.
func stripDetails(_ ev: EKEvent) {
    ev.notes = nil
    ev.location = nil
    ev.structuredLocation = nil
    ev.url = nil
}

func statusName(_ s: EKEventStatus) -> String {
    switch s {
    case .confirmed: return "confirmed"
    case .tentative: return "tentative"
    case .canceled: return "canceled"
    default: return "none"
    }
}

// MARK: - Commands

func cmdAuth() -> Never {
    let (granted, err) = requestAccess()
    emit(["ok": granted, "granted": granted, "error": err ?? ""])
}

/// Report what this bundle was built from. Deliberately requires no calendar
/// access, so it still answers when permission is missing or revoked.
func cmdVersion() -> Never {
    let info = Bundle.main.infoDictionary ?? [:]
    emit([
        "ok": true,
        "version": info["CFBundleShortVersionString"] as? String ?? "unknown",
        "buildDate": info["CalSyncBuildDate"] as? String ?? "",
        "commit": info["CalSyncGitCommit"] as? String ?? "",
    ])
}

func cmdCalendars() -> Never {
    requireAccess()
    let cals = store.calendars(for: .event).map(describe)
    emit(["ok": true, "calendars": cals])
}

func cmdEvents() -> Never {
    let input = readInputJSON()
    requireAccess()

    let ids = input["calendarIds"] as? [String] ?? []
    let days = input["days"] as? Int ?? 60
    let all = store.calendars(for: .event)
    let selected = ids.isEmpty ? all : all.filter { ids.contains($0.calendarIdentifier) }

    if selected.isEmpty { fail("none of the requested calendar ids exist") }

    // Start slightly in the past so an event that began earlier today is still seen.
    let start = Calendar.current.startOfDay(for: Date())
    guard let end = Calendar.current.date(byAdding: .day, value: days, to: start) else {
        fail("could not compute the end of the window")
    }

    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: selected)
    let events = store.events(matching: predicate).map(describe)
    emit(["ok": true, "events": events, "windowStart": iso.string(from: start), "windowEnd": iso.string(from: end)])
}

func cmdCreate() -> Never {
    let input = readInputJSON()
    requireAccess()

    guard let calId = input["calendarId"] as? String,
          let cal = store.calendar(withIdentifier: calId) else {
        fail("unknown target calendarId")
    }
    guard cal.allowsContentModifications else {
        fail("calendar '\(cal.title)' is read-only")
    }
    guard let start = parseDate(input["start"]), let end = parseDate(input["end"]) else {
        fail("start and end must be ISO-8601 timestamps")
    }

    let ev = EKEvent(eventStore: store)
    ev.calendar = cal
    ev.title = (input["title"] as? String) ?? "Busy"
    ev.startDate = start
    ev.endDate = end
    ev.isAllDay = (input["allDay"] as? Bool) ?? false
    // The whole point of the mirror: show the time as taken, and nothing else.
    markBusy(ev)
    stripDetails(ev)

    do {
        try store.save(ev, span: .thisEvent, commit: true)
    } catch {
        fail("could not save event: \(error.localizedDescription)")
    }
    emit(["ok": true, "eventId": ev.eventIdentifier ?? "", "calendarTitle": cal.title])
}

func cmdUpdate() -> Never {
    let input = readInputJSON()
    requireAccess()

    guard let eventId = input["eventId"] as? String else { fail("eventId is required") }
    guard let ev = store.event(withIdentifier: eventId) else {
        // Not an error: the block was deleted by hand. Report it as a fact the
        // caller can branch on, the same way delete does, rather than as a
        // failure — callers need to tell "gone" apart from "could not write".
        emit(["ok": true, "missing": true])
    }
    if let t = input["title"] as? String { ev.title = t }
    if let s = parseDate(input["start"]) { ev.startDate = s }
    if let e = parseDate(input["end"]) { ev.endDate = e }
    if let a = input["allDay"] as? Bool { ev.isAllDay = a }
    markBusy(ev)
    // Every field is optional, so an update carrying only an eventId is a
    // scrub: it leaves the time and title alone and clears the detail fields.
    stripDetails(ev)

    do {
        try store.save(ev, span: .thisEvent, commit: true)
    } catch {
        fail("could not update event: \(error.localizedDescription)")
    }
    emit(["ok": true, "eventId": ev.eventIdentifier ?? ""])
}

func cmdDelete() -> Never {
    let input = readInputJSON()
    requireAccess()

    guard let eventId = input["eventId"] as? String else { fail("eventId is required") }
    guard let ev = store.event(withIdentifier: eventId) else {
        // Already gone — that is the state the caller wanted.
        emit(["ok": true, "missing": true])
    }
    do {
        try store.remove(ev, span: .thisEvent, commit: true)
    } catch {
        fail("could not delete event: \(error.localizedDescription)")
    }
    emit(["ok": true])
}

/// Post a Notification Centre banner under this bundle's own identity.
///
/// `osascript -e 'display notification'` posts as Script Editor, which on a
/// stock system is not registered with Notification Centre at all — it exits 0
/// and the banner is silently dropped. Posting from a signed bundle gives
/// CalSync its own entry in System Settings > Notifications, so the banner
/// actually appears and the user can configure it.
func cmdNotify() -> Never {
    let input = readInputJSON()
    let title = (input["title"] as? String) ?? "CalSync"
    let body = (input["body"] as? String) ?? ""

    // UNUserNotificationCenter talks to the notification daemon through the
    // application object and the main run loop. A plain command-line process
    // has neither, so requests are accepted and then quietly go nowhere —
    // blocking on a semaphore here would deadlock the very loop that delivers
    // the result. Hence a real NSApplication and a genuine run loop.
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    let centre = UNUserNotificationCenter.current()
    var finished = false
    var failure: String? = nil

    centre.requestAuthorization(options: [.alert, .sound]) { granted, err in
        if let err = err {
            failure = err.localizedDescription
            finished = true
            return
        }
        guard granted else {
            failure = "notifications are turned off for CalSync. Enable them in"
                + " System Settings > Notifications > CalSyncBridge."
            finished = true
            return
        }

        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: UUID().uuidString, content: content, trigger: nil
        )
        centre.add(request) { addError in
            if let addError = addError { failure = addError.localizedDescription }
            finished = true
        }
    }

    // The first call shows a system prompt the user has to answer.
    let deadline = Date().addingTimeInterval(60)
    while !finished && Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
    // Let the daemon collect the request before this process goes away.
    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.7))

    if let failure = failure { fail(failure) }
    if !finished { fail("timed out posting the notification") }
    emit(["ok": true])
}

// MARK: - Entry point

var positional: [String] = []
var argv = Array(CommandLine.arguments.dropFirst())
var i = 0
while i < argv.count {
    switch argv[i] {
    case "--in":
        i += 1
        if i < argv.count { inputPath = argv[i] }
    case "--out":
        i += 1
        if i < argv.count { outputPath = argv[i] }
    // LaunchServices appends -psn_x_y when it launches a bundle; ignore it.
    case let a where a.hasPrefix("-psn_"):
        break
    default:
        positional.append(argv[i])
    }
    i += 1
}

guard let command = positional.first else {
    fail("usage: calbridge [--in FILE] [--out FILE] <auth|version|calendars|events|create|update|delete|notify>")
}

switch command {
case "auth": cmdAuth()
case "version": cmdVersion()
case "calendars": cmdCalendars()
case "events": cmdEvents()
case "create": cmdCreate()
case "update": cmdUpdate()
case "delete": cmdDelete()
case "notify": cmdNotify()
default: fail("unknown command '\(command)'")
}
