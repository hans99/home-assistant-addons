# Home Assistant Add-on: VirtualHere Client

## About
Uses the client build in API with the -t argument.
For more information, see [[VirtualHere Client API documentation](https://www.virtualhere.com/client_api)].
Client configuration file is stored in home assistant config directory and named vhclient.ini, for easy access.

## How to use
Default this will use use the client build in server discovery and auto use all devices. To change this or handle configuration manually, use Configuration as descirbed.

## Configuration

### Option: 'AUTOUSE'
Turn auto-use all devices on / off. Corresponds to client API "AUTO USE ALL", but edits the vhclient.ini directly (Section AutoShare, value All)

### Option: 'AUTOFIND'
Turn auto-find on / off. Corresponds to client API "AUTOFIND", but edits the vhclient.ini directly (Section General, value AutoFind).

### Option: 'USB devices'
This present a list of all devices on the server, reload the page to get current updated list. Change 'use' to 'true/false' for devices to be used. Any device removed from the server permanently can be manually deleted here.

### Option: 'No client config'
Activating this prevents the addon to touch the vhclient.ini file, to allow for total manual control.

### Option: 'ClientId'
This adds a custom 'In use by ...' on the server. Edits the vhclient.ini (Section General, value ClientId).

### Advanced Option: 'MANUAL HUB'
Specify server hub to connect to. Corresponds to client API "MANUAL HUB ADD", but edits the vhclient.ini directly (Section Settings, value ManualHubs). Both IP and port is needed.
