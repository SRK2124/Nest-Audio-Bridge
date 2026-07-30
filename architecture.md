sequenceDiagram
    autonumber
    
    participant App as Python Script / Mobile App
    participant Server as Local HTTP Server
    participant Nest as Google Nest Speaker

    Note over App, Server: Device Preparation
    App->>Server: Spin up temporary background server
    Note right of Server: Exposes file at http://192.168.x.x:8080/song.mp3

    Note over App, Nest: Handshake & App Launch
    App->>Nest: Open secure connection (TCP/TLS on Port 8009)
    App->>Nest: Launch "Default Media Receiver" (App ID CC1AD845)
    Nest-->>App: Receiver is active and ready

    Note over App, Nest: Playback Command
    App->>Nest: Send LOAD command with local URL
    
    Note over Server, Nest: Media Streaming
    Nest->>Server: HTTP GET /song.mp3
    Server-->>Nest: Stream audio data chunks (HTTP 200)
    Note right of Nest: Nest begins buffering and playing
    
    Note over App, Nest: Session Maintenance
    loop Continuous Heartbeat
        App->>Nest: PING
        Nest-->>App: PONG
    end