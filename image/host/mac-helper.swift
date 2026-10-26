import Darwin
import DiskArbitration
import Foundation
import IOKit
import Virtualization

// A separate guardian keeps mount approval active while the VM drops privilege.
final class MountGuard {
  let session: DASession
  let registryID: UInt64
  init(_ name: String?) throws {
    guard let session = DASessionCreate(kCFAllocatorDefault) else {
      throw NSError(domain: "FasciaGuard", code: 1)
    }
    self.session = session
    if let name = name {
      guard name.range(of: "^disk[0-9]+$", options: .regularExpression) != nil,
        let disk = DADiskCreateFromBSDName(kCFAllocatorDefault, session, name),
        let whole = DADiskCopyWholeDisk(disk),
        let bsd = DADiskGetBSDName(whole), String(cString: bsd) == name
      else {
        throw NSError(domain: "FasciaGuard", code: 2)
      }
      let media = DADiskCopyIOMedia(whole)
      defer { IOObjectRelease(media) }
      var identifier: UInt64 = 0
      guard media != 0, IORegistryEntryGetRegistryEntryID(media, &identifier) == KERN_SUCCESS else {
        throw NSError(domain: "FasciaGuard", code: 3)
      }
      registryID = identifier
    } else {
      registryID = 0
    }  // Registration-only fixture; never matches a disk.
  }
  func start() {
    DARegisterDiskMountApprovalCallback(
      session, nil,
      { disk, context in
        guard let context = context else { return nil }
        let owner = Unmanaged<MountGuard>.fromOpaque(context).takeUnretainedValue()
        guard owner.registryID != 0, let whole = DADiskCopyWholeDisk(disk) else { return nil }
        let media = DADiskCopyIOMedia(whole)
        defer { IOObjectRelease(media) }
        var identifier: UInt64 = 0
        guard media != 0, IORegistryEntryGetRegistryEntryID(media, &identifier) == KERN_SUCCESS,
          identifier == owner.registryID
        else { return nil }
        return Unmanaged.passRetained(
          DADissenterCreate(
            kCFAllocatorDefault, DAReturn(kDAReturnBusy),
            "Fascia installation is using this card" as CFString))
      }, Unmanaged.passUnretained(self).toOpaque())
    DASessionSetDispatchQueue(session, DispatchQueue.main)
    // Queue one turn after scheduling before reporting ready.
    DispatchQueue.main.async {
      print("FASCIA_GUARD_READY")
      fflush(stdout)
    }
    DispatchQueue.global().async {
      while let line = readLine() {
        if line == "ping" {
          print("FASCIA_GUARD_ALIVE")
          fflush(stdout)
        } else if line == "stop" {
          exit(0)
        } else {
          exit(2)
        }
      }
      exit(0)  // Parent death/closed lifetime pipe releases the session.
    }
  }
}
// DHCP's vmnet DNS proxy can be unavailable with host VPN resolvers. Snapshot
// non-loopback IPv4 resolver addresses, never search domains or authentication data.
func hostResolvers() -> [String] {
  guard let text = try? String(contentsOfFile: "/etc/resolv.conf", encoding: .utf8) else {
    return []
  }
  var addresses: [String] = []
  for line in text.split(separator: "\n") {
    let words = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
    guard words.count >= 2, words[0] == "nameserver" else { continue }
    let address = String(words[1])
    var parsed = in_addr()
    guard inet_pton(AF_INET, address, &parsed) == 1,
      !address.hasPrefix("127."), !address.hasPrefix("0."), !address.hasPrefix("169.254."),
      !addresses.contains(address)
    else { continue }
    addresses.append(address)
    if addresses.count == 2 { break }
  }
  return addresses
}
// Device validation/unmount/approval belongs to the privileged host opener.
// This worker receives an already-open descriptor; it never opens a device path.
final class Worker: NSObject, VZVirtualMachineDelegate {
  let machine: VZVirtualMachine
  init(_ args: [String]) throws {
    guard args.count == 9, let capacity = UInt64(args[5]),
      ["FASCIA-INSTALL-FILE", "FASCIA-INSTALL-CARD", "FASCIA-PREPARE"].contains(args[6])
    else {
      throw NSError(
        domain: "Fascia", code: 1, userInfo: [NSLocalizedDescriptionKey: "Invalid worker arguments"])
    }
    let preparing = args[6] == "FASCIA-PREPARE"
    guard
      preparing
        ? (args[7] == "none" && args[8] == "-" && capacity == 0)
        : (["image", "fd"].contains(args[7]) && capacity > 0)
    else {
      throw NSError(
        domain: "Fascia", code: 6,
        userInfo: [
          NSLocalizedDescriptionKey:
            "Preparation must have no disk; installation requires one explicit target"
        ])
    }
    let config = VZVirtualMachineConfiguration()
    let loader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: args[1]))
    loader.initialRamdiskURL = URL(fileURLWithPath: args[2])
    loader.commandLine =
      "console=hvc0 rdinit=/init panic=-1 fascia_capacity=\(capacity) fascia_serial=\(args[6])"
    if preparing {
      let resolvers = hostResolvers()
      if !resolvers.isEmpty {
        loader.commandLine += " fascia_dns=" + resolvers.joined(separator: ",")
      }
    }
    config.bootLoader = loader
    config.cpuCount = min(
      ProcessInfo.processInfo.activeProcessorCount,
      VZVirtualMachineConfiguration.maximumAllowedCPUCount)
    config.memorySize = min(
      8 * 1024 * 1024 * 1024, VZVirtualMachineConfiguration.maximumAllowedMemorySize)
    let console = VZVirtioConsoleDeviceSerialPortConfiguration()
    console.attachment = VZFileHandleSerialPortAttachment(
      fileHandleForReading: nil, fileHandleForWriting: .standardOutput)
    config.serialPorts = [console]
    config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
    config.networkDevices = []
    if preparing {
      let network = VZVirtioNetworkDeviceConfiguration()
      network.attachment = VZNATNetworkDeviceAttachment()
      config.networkDevices = [network]
    }
    for (tag, path, readOnly) in [("fascia-input", args[3], true), ("fascia-output", args[4], false)]
    {
      var directory: ObjCBool = false
      guard FileManager.default.fileExists(atPath: path, isDirectory: &directory),
        directory.boolValue
      else {
        throw NSError(
          domain: "Fascia", code: 2,
          userInfo: [NSLocalizedDescriptionKey: "Share is not a directory"])
      }
      let fs = VZVirtioFileSystemDeviceConfiguration(tag: tag)
      fs.share = VZSingleDirectoryShare(
        directory: VZSharedDirectory(url: URL(fileURLWithPath: path), readOnly: readOnly))
      config.directorySharingDevices.append(fs)
    }
    if args[7] != "none" {
      let attachment: VZStorageDeviceAttachment
      if args[7] == "fd", let fd = Int32(args[8]) {
        var info = stat()
        guard fstat(fd, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFCHR || (info.st_mode & S_IFMT) == S_IFBLK,
          args[6] == "FASCIA-INSTALL-CARD"
        else {
          throw NSError(
            domain: "Fascia", code: 3,
            userInfo: [NSLocalizedDescriptionKey: "Expected inherited device descriptor"])
        }
        attachment = try VZDiskBlockDeviceStorageDeviceAttachment(
          fileHandle: FileHandle(fileDescriptor: fd, closeOnDealloc: false), readOnly: false,
          synchronizationMode: .full)
      } else if args[7] == "image" {
        let path = URL(fileURLWithPath: args[8])
        let info = try FileManager.default.attributesOfItem(atPath: path.path)
        guard info[.type] as? FileAttributeType == .typeRegular,
          (info[.size] as? NSNumber)?.uint64Value == capacity,
          args[6] == "FASCIA-INSTALL-FILE"
        else {
          throw NSError(
            domain: "Fascia", code: 4,
            userInfo: [NSLocalizedDescriptionKey: "Expected capacity-matched regular image"])
        }
        attachment = try VZDiskImageStorageDeviceAttachment(url: path, readOnly: false)
      } else {
        throw NSError(
          domain: "Fascia", code: 5, userInfo: [NSLocalizedDescriptionKey: "Invalid target mode"])
      }
      let block = VZVirtioBlockDeviceConfiguration(attachment: attachment)
      block.blockDeviceIdentifier = args[6]
      config.storageDevices = [block]
    }
    try config.validate()
    machine = VZVirtualMachine(configuration: config)
    super.init()
    machine.delegate = self
  }
  func start() {
    machine.start { result in
      if case .failure(let error) = result {
        fputs("VM start: \(error)\n", stderr)
        exit(1)
      }
    }
  }
  func guestDidStop(_ virtualMachine: VZVirtualMachine) { exit(0) }
  func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
    fputs("VM failed: \(error)\n", stderr)
    exit(1)
  }
}
do {
  if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--guard" {
    let guarder = try MountGuard(
      CommandLine.arguments[2] == "probe" ? nil : CommandLine.arguments[2])
    guarder.start()
    withExtendedLifetime(guarder) { RunLoop.main.run() }
    exit(0)
  }
  let worker = try Worker(CommandLine.arguments)
  worker.start()
  withExtendedLifetime(worker) { RunLoop.main.run() }
} catch {
  fputs("Fascia helper: \(error)\n", stderr)
  exit(1)
}
